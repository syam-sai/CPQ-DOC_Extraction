# app.py
#
# Streamlit UI for Salesforce CPQ Configuration Extractor & Client Document Generator.
#
# Key Features:
#   - Connects via Client Credentials or Username/Password
#   - Local-first run folders with streaming outputs (NDJSON, JSON, 21-tab XLSX)
#   - Client Document Generator (.docx) powered by doc_gen CLI with live status polling
#   - Memory efficient: saved artifacts are streamed/read from disk; reruns never rebuild data

import os
import sys
import time
import json
import subprocess
from datetime import datetime, timezone

import streamlit as st
import config
import sf_auth
import extractor
import transformer
import storage
import xlsx_export

st.set_page_config(page_title="CPQ Config Extractor & Document Generator", layout="wide")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "instances" not in st.session_state:
    st.session_state.instances = {}
if "selected_instance" not in st.session_state:
    st.session_state.selected_instance = None
if "active_run_dir" not in st.session_state:
    st.session_state.active_run_dir = None
if "run_metadata" not in st.session_state:
    st.session_state.run_metadata = {}


# ---------------------------------------------------------------------------
# Relationship diagram (annotated with live counts)
# ---------------------------------------------------------------------------
def build_relationship_graph(counts):
    return f"""
    digraph CPQ {{
        rankdir=TB;
        node [shape=box, style="filled,rounded", fontname="Helvetica"];

        Product2 [label="Product2\\n({counts.get('Product', 0)} active)", fillcolor="#cfe3ff"];
        Pricing [label="Pricing objects\\nblock price, cost, dimension...", fillcolor="#d9f2d9"];
        Features [label="Features\\n({counts.get('Features', 0)})", fillcolor="#ffe8cc"];
        Options [label="Options\\n({counts.get('Options', 0)})", fillcolor="#ffe8cc"];
        ProductRule [label="Product Rule\\n({counts.get('ProductRules', 0)} active)", fillcolor="#e6ccff"];
        PriceRule [label="Price Rule\\n({counts.get('PriceRules', 0)} active)", fillcolor="#e6ccff"];
        SummaryVar [label="Summary Variable\\n({counts.get('SummaryVariables', 0)})", fillcolor="#eeeeee"];

        Product2 -> Pricing;
        Product2 -> Features;
        Features -> Options [label="grouped"];
        Product2 -> Options [label="direct", style=dashed];
        Product2 -> PriceRule [label="scoped", style=dashed];
        ProductRule -> SummaryVar [label="reads", style=dotted];
        PriceRule -> SummaryVar [label="reads", style=dotted];
    }}
    """


def find_existing_runs():
    """Finds all existing completed runs in data/runs."""
    base = os.path.abspath("data/runs")
    if not os.path.exists(base):
        return []
    runs = []
    for inst in os.listdir(base):
        inst_dir = os.path.join(base, inst)
        if os.path.isdir(inst_dir):
            for r in os.listdir(inst_dir):
                r_dir = os.path.join(inst_dir, r)
                status_file = os.path.join(r_dir, "status.json")
                out_xlsx = os.path.join(r_dir, "output", "cpq_extract.xlsx")
                if os.path.exists(status_file) or os.path.exists(out_xlsx):
                    runs.append((f"{inst} / {r}", r_dir))
    return sorted(runs, reverse=True)


# ---------------------------------------------------------------------------
# Sidebar — instances & existing runs
# ---------------------------------------------------------------------------
st.sidebar.header("Salesforce Instances")
st.sidebar.caption(f"Mode: `{config.APP_ENV}`")

# Manual add form
st.sidebar.subheader("Add Instance")
auth_method_label = st.sidebar.radio(
    "Auth method",
    ["Username + password (works now)", "Client credentials (use once available)"],
    key="new_instance_auth_method",
)

with st.sidebar.form("add_instance_form", clear_on_submit=True):
    name = st.text_input("Instance name (label)")
    domain = st.selectbox("Domain", ["login", "test"], help="'login' for production, 'test' for sandbox")

    if auth_method_label.startswith("Username"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        token = st.text_input("Security token (leave blank if not required)", type="password")
    else:
        client_id = st.text_input("Consumer key")
        client_secret = st.text_input("Consumer secret", type="password")

    submitted = st.form_submit_button("Add instance")
    if submitted:
        if not name:
            st.sidebar.error("Instance name is required.")
        elif name in st.session_state.instances:
            st.sidebar.error("An instance with this name already exists.")
        elif auth_method_label.startswith("Username") and (not username or not password):
            st.sidebar.error("Username and password are required.")
        elif auth_method_label.startswith("Client") and (not client_id or not client_secret):
            st.sidebar.error("Consumer key and secret are required.")
        else:
            if auth_method_label.startswith("Username"):
                st.session_state.instances[name] = {
                    "auth_method": "password",
                    "username": username,
                    "password": password,
                    "security_token": token,
                    "domain": domain,
                }
            else:
                st.session_state.instances[name] = {
                    "auth_method": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "domain": domain,
                }
            st.sidebar.success(f"Added '{name}'")

# AWS Secrets Manager loader
st.sidebar.divider()
with st.sidebar.expander("Load from AWS Secrets Manager"):
    st.caption(f"Looks for secrets named '{config.SECRETS_PREFIX}*' in region {config.AWS_REGION}.")
    if st.button("Refresh from Secrets Manager"):
        try:
            secret_names = storage.list_secret_instances(config.SECRETS_PREFIX, config.AWS_REGION)
            for secret_name in secret_names:
                display_name = secret_name.replace(config.SECRETS_PREFIX, "")
                instance = storage.get_secret_instance(secret_name, config.AWS_REGION)
                st.session_state.instances[display_name] = instance
            st.success(f"Loaded {len(secret_names)} instance(s).")
        except Exception as e:
            st.error(f"Secrets Manager error: {e}")

# Instance List
if st.session_state.instances:
    st.sidebar.divider()
    st.sidebar.subheader("Configured Instances")
    for inst_name, inst in list(st.session_state.instances.items()):
        col1, col2 = st.sidebar.columns([3, 1])
        badge = "🔑" if inst["auth_method"] == "client_credentials" else "🔓"
        if col1.button(f"{badge} {inst_name}", key=f"select_{inst_name}", use_container_width=True):
            st.session_state.selected_instance = inst_name
        if col2.button("🗑", key=f"delete_{inst_name}"):
            del st.session_state.instances[inst_name]
            if st.session_state.selected_instance == inst_name:
                st.session_state.selected_instance = None
            st.rerun()

# Existing Runs Browser
existing_runs = find_existing_runs()
if existing_runs:
    st.sidebar.divider()
    st.sidebar.subheader("Saved Run Folders")
    selected_run_label = st.sidebar.selectbox(
        "Browse past runs",
        ["-- Select an existing run --"] + [label for label, _ in existing_runs],
        key="selected_existing_run"
    )
    if selected_run_label != "-- Select an existing run --":
        run_path = dict(existing_runs)[selected_run_label]
        if st.sidebar.button("📂 Load Selected Run", use_container_width=True):
            st.session_state.active_run_dir = run_path
            # Load metadata
            status_file = os.path.join(run_path, "status.json")
            if os.path.exists(status_file):
                with open(status_file, "r", encoding="utf-8") as f:
                    st.session_state.run_metadata = json.load(f)
            st.rerun()


# ---------------------------------------------------------------------------
# Main Area
# ---------------------------------------------------------------------------
st.title("Salesforce CPQ Extractor & Document Generator")

selected = st.session_state.selected_instance

if selected and selected in st.session_state.instances:
    instance = st.session_state.instances[selected]
    st.subheader(f"Connected Instance: {selected}")
    method_display = "Client credentials" if instance["auth_method"] == "client_credentials" else "Username + password"
    st.caption(f"Auth: `{method_display}` · Domain: `{instance['domain']}`")

    if st.button("🔌 Connect & Extract Data", type="primary"):
        import importlib
        import extractor
        import transformer
        import sf_auth
        importlib.reload(extractor)
        importlib.reload(transformer)
        importlib.reload(sf_auth)

        # Force clear all in-memory caches and session state for a 100% fresh run
        extractor.clear_describe_cache()
        st.session_state.active_run_dir = None
        st.session_state.run_metadata = {}

        status_box = st.empty()
        progress_bar = st.progress(0.0)

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.abspath(os.path.join("data", "runs", selected, run_id))
        os.makedirs(os.path.join(run_dir, "output"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "raw"), exist_ok=True)

        try:
            status_box.info("Connecting to Salesforce...")
            progress_bar.progress(0.1)
            sf = sf_auth.connect(instance)

            def progress_cb(msg):
                status_box.info(msg)

            extract_log = {}
            data, counts = extractor.extract_all(
                sf,
                progress_callback=progress_cb,
                run_dir=run_dir,
                extract_log=extract_log,
                force_fresh=True,
            )
            progress_bar.progress(0.6)

            status_box.info("Staging and streaming 21-tab Excel & JSON with DuckDB...")
            counts, trunc_log = transformer.transform_run(
                run_dir=run_dir,
                progress_callback=progress_cb,
            )
            progress_bar.progress(1.0)

            # Auto-upload to S3 if configured
            s3_path = None
            if config.S3_BUCKET:
                status_box.info("Uploading to S3...")
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                key = f"extracts/{selected}/{timestamp}.json"
                s3_path = storage.save_to_s3(data, config.S3_BUCKET, key, config.AWS_REGION)

            st.session_state.active_run_dir = run_dir
            st.session_state.run_metadata = {
                "instance": selected,
                "counts": counts,
                "s3_path": s3_path,
                "run_dir": run_dir,
            }
            status_box.success("Extraction complete! Staged in run folder.")

        except Exception as e:
            status_box.error(f"Extraction failed: {e}")

elif not st.session_state.active_run_dir:
    st.info("Add a Salesforce instance from the sidebar or select a saved run folder to begin.")


# ---------------------------------------------------------------------------
# Results & Client Document Generation View
# ---------------------------------------------------------------------------
active_run = st.session_state.active_run_dir

if active_run and os.path.exists(active_run):
    meta = st.session_state.run_metadata or {}
    counts = meta.get("counts") or {}

    out_json = os.path.join(active_run, "output", "cpq_extract.json")
    out_ndjson = os.path.join(active_run, "output", "cpq_extract.ndjson")
    out_xlsx = os.path.join(active_run, "output", "cpq_extract.xlsx")
    doc_docx = os.path.join(active_run, "doc", "client_document.docx")
    doc_status = os.path.join(active_run, "doc", "status.json")

    # If counts not loaded, read facts or status
    if not counts and os.path.exists(out_json):
        from doc_gen.facts import compute_facts
        facts = compute_facts(out_json)
        counts = {
            "Product": facts.get("active_products", 0),
            "ProductRules": facts.get("product_rules", {}).get("total", 0),
            "PriceRules": facts.get("price_rules", {}).get("total", 0),
            "SummaryVariables": facts.get("summary_variables", {}).get("total", 0),
            "Features": facts.get("total_features", 0),
            "Options": facts.get("total_options", 0),
        }

    st.divider()
    st.subheader(f"Active Run: `{os.path.basename(os.path.dirname(active_run))} / {os.path.basename(active_run)}`")

    # Relationship Map
    if counts:
        st.markdown("### Object Relationship Map")
        st.graphviz_chart(build_relationship_graph(counts), use_container_width=True)

        st.markdown("### Catalog Summary")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Products", counts.get("Product", 0))
        c2.metric("Product Rules", counts.get("ProductRules", 0))
        c3.metric("Price Rules", counts.get("PriceRules", 0))
        c4.metric("Summary Variables", counts.get("SummaryVariables", 0))

    if meta.get("s3_path"):
        st.caption(f"Archived to S3: `{meta['s3_path']}`")

    # Raw Artifact Downloads
    st.markdown("### Technical Downloads")
    d_col1, d_col2 = st.columns(2)

    if os.path.exists(out_json):
        with open(out_json, "rb") as f_json:
            d_col1.download_button(
                label="⬇️ Download Full JSON",
                data=f_json.read(),
                file_name="cpq_extract.json",
                mime="application/json",
                use_container_width=True,
            )

    if os.path.exists(out_xlsx):
        with open(out_xlsx, "rb") as f_xlsx:
            d_col2.download_button(
                label="⬇️ Download Excel (21 Tabs)",
                data=f_xlsx.read(),
                file_name="cpq_extract.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

    # -------------------------------------------------------------
    # CLIENT DOCUMENT GENERATION (.docx)
    # -------------------------------------------------------------
    st.divider()
    st.markdown("### 📄 Executive Client Document Generator")
    st.caption("Transforms technical CPQ rules, bundles, and pricing into a polished, non-technical Word document (.docx) for clients and executives.")

    doc_col1, doc_col2 = st.columns([2, 1])
    client_name_input = doc_col1.text_input(
        "Client / Company Name",
        value=meta.get("instance") or "Acme Corporation",
        help="Appears on the document cover page and headers."
    )

    use_mock_llm = doc_col2.checkbox(
        "Offline Mock Mode (No LLM API keys needed)",
        value=True if not os.environ.get("LLM_API_KEY") and not os.environ.get("GEMINI_API_KEY") else False,
        help="Uses the built-in deterministic offline engine."
    )

    include_mig_notes = doc_col2.checkbox("Include Migration Notes", value=False)

    if st.button("🚀 Generate Client Document (.docx)", type="primary"):
        doc_status_box = st.empty()
        doc_progress_bar = st.progress(0.0)

        # Launch run_document.py as subprocess
        cmd = [
            sys.executable,
            "run_document.py",
            "--run", active_run,
            "--client-name", client_name_input,
        ]
        if use_mock_llm:
            cmd.append("--mock-llm")
        if include_mig_notes:
            cmd.append("--include-migration-notes")

        doc_status_box.info("Launching document generation engine in background...")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Poll status.json
        completed = False
        while not completed:
            time.sleep(0.5)
            if proc.poll() is not None:
                # Subprocess finished
                completed = True
                break

            if os.path.exists(doc_status):
                try:
                    with open(doc_status, "r", encoding="utf-8") as sf:
                        s_data = json.load(sf)
                        st_state = s_data.get("state")
                        st_step = s_data.get("current_step", "")
                        st_prog = float(s_data.get("progress", 0.5))
                        doc_progress_bar.progress(min(max(st_prog, 0.0), 1.0))
                        doc_status_box.info(st_step)
                        if st_state in ("completed", "failed"):
                            completed = True
                except Exception:
                    pass

        stdout, stderr = proc.communicate()
        if proc.returncode == 0 and os.path.exists(doc_docx):
            doc_progress_bar.progress(1.0)
            doc_status_box.success("Client document generated successfully!")
            st.rerun()
        else:
            doc_status_box.error(f"Generation failed: {stderr or stdout}")

    # If client document is ready, offer download
    if os.path.exists(doc_docx):
        st.markdown("#### Ready for Download")
        with open(doc_docx, "rb") as f_docx:
            st.download_button(
                label=f"⬇️ Download Client Document ({os.path.basename(doc_docx)})",
                data=f_docx.read(),
                file_name=f"{client_name_input.replace(' ', '_')}_CPQ_Overview.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )

        val_file = os.path.join(active_run, "doc", "validation_report.json")
        if os.path.exists(val_file):
            with open(val_file, "r", encoding="utf-8") as f_val:
                v_rep = json.load(f_val)
                if v_rep.get("can_render"):
                    st.caption("✅ Passed strict security regex check (0 Salesforce IDs or SBQQ__ tags in body text).")
                else:
                    st.warning("⚠️ Document contained flagged items; see validation report.")