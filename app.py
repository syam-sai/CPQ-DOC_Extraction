# app.py
#
# Run locally:   streamlit run app.py
# Run in AWS:    same command, inside the container (see Dockerfile) —
#                behavior adapts automatically based on config.py's
#                environment variables (APP_ENV, S3_BUCKET, etc).
#
# Credentials entered manually via the sidebar form live ONLY in this
# session's memory — nothing is written to disk locally. In AWS mode,
# credentials can instead be loaded from Secrets Manager and are never
# typed into the UI at all.

import streamlit as st
import json
from datetime import datetime, timezone

import io
import config
import sf_auth
import extractor
import storage
import xlsx_export

st.set_page_config(page_title="CPQ Config Extractor", layout="wide")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "instances" not in st.session_state:
    st.session_state.instances = {}   # name -> instance dict (see sf_auth.connect)
if "selected_instance" not in st.session_state:
    st.session_state.selected_instance = None
if "extract_results" not in st.session_state:
    st.session_state.extract_results = {}  # name -> {"data": ..., "counts": ...}


# ---------------------------------------------------------------------------
# Relationship diagram (static structure, annotated with live counts)
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


# ---------------------------------------------------------------------------
# Sidebar — instance manager
# ---------------------------------------------------------------------------
st.sidebar.header("Salesforce instances")
st.sidebar.caption(f"Mode: `{config.APP_ENV}`")

# --- Manual add form ---
st.sidebar.subheader("Add instance manually")

auth_method_label = st.sidebar.radio(
    "Auth method",
    ["Username + password (works now)", "Client credentials (use once available)"],
    key="new_instance_auth_method",
    help="Salesforce is retiring the username+password flow — switch to "
         "client credentials as soon as your admin can create the Connected App.",
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

st.sidebar.caption("Manually entered credentials live only in this session's memory.")

# --- AWS Secrets Manager loader (only relevant in aws mode, but shown either
#     way in case someone wants to test against Secrets Manager locally too) ---
st.sidebar.divider()
with st.sidebar.expander("Load instances from AWS Secrets Manager"):
    st.caption(f"Looks for secrets named '{config.SECRETS_PREFIX}*' in region {config.AWS_REGION}.")
    if st.button("Refresh from Secrets Manager"):
        try:
            secret_names = storage.list_secret_instances(config.SECRETS_PREFIX, config.AWS_REGION)
            if not secret_names:
                st.warning("No matching secrets found.")
            for secret_name in secret_names:
                display_name = secret_name.replace(config.SECRETS_PREFIX, "")
                instance = storage.get_secret_instance(secret_name, config.AWS_REGION)
                st.session_state.instances[display_name] = instance
            st.success(f"Loaded {len(secret_names)} instance(s) from Secrets Manager.")
        except Exception as e:
            st.error(f"Could not reach Secrets Manager: {e}")

# --- Instance list ---
st.sidebar.divider()
if st.session_state.instances:
    st.sidebar.subheader("Your instances")
    for inst_name, inst in list(st.session_state.instances.items()):
        col1, col2 = st.sidebar.columns([3, 1])
        badge = "🔑" if inst["auth_method"] == "client_credentials" else "🔓"
        if col1.button(f"{badge} {inst_name}", key=f"select_{inst_name}", use_container_width=True):
            st.session_state.selected_instance = inst_name
        if col2.button("🗑", key=f"delete_{inst_name}"):
            del st.session_state.instances[inst_name]
            st.session_state.extract_results.pop(inst_name, None)
            if st.session_state.selected_instance == inst_name:
                st.session_state.selected_instance = None
            st.rerun()

# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------
st.title("Salesforce CPQ configuration extractor")

if not st.session_state.instances:
    st.info("Add a Salesforce instance from the sidebar to get started.")
else:
    selected = st.session_state.selected_instance
    if not selected:
        st.info("Select an instance from the sidebar.")
    else:
        instance = st.session_state.instances[selected]
        st.subheader(f"Instance: {selected}")
        method_display = "Client credentials" if instance["auth_method"] == "client_credentials" else "Username + password"
        st.caption(f"Auth: `{method_display}` · Domain: `{instance['domain']}`")

        if st.button("🔌 Connect & extract data", type="primary"):
            status = st.empty()
            try:
                status.info("Connecting to Salesforce...")
                sf = sf_auth.connect(instance)

                def progress(msg):
                    status.info(msg)

                data, counts = extractor.extract_all(sf, progress_callback=progress)
                st.session_state.extract_results[selected] = {"data": data, "counts": counts}

                # Auto-upload to S3 if configured
                if config.S3_BUCKET:
                    status.info("Uploading to S3...")
                    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    key = f"extracts/{selected}/{timestamp}.json"
                    s3_path = storage.save_to_s3(data, config.S3_BUCKET, key, config.AWS_REGION)
                    st.session_state.extract_results[selected]["s3_path"] = s3_path

                status.success("Extraction complete.")
            except Exception as e:
                status.error(f"Failed: {e}")

        if selected in st.session_state.extract_results:
            bundle = st.session_state.extract_results[selected]
            counts = bundle["counts"]
            data = bundle["data"]

            st.markdown("### Object relationship map")
            st.graphviz_chart(build_relationship_graph(counts), use_container_width=True)

            st.markdown("### Summary")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Products", counts["Product"])
            c2.metric("Product Rules", counts["ProductRules"])
            c3.metric("Price Rules", counts["PriceRules"])
            c4.metric("Summary Variables", counts["SummaryVariables"])

            if bundle.get("s3_path"):
                st.caption(f"Also saved to `{bundle['s3_path']}`")

            st.markdown("### Download")

            json_str = json.dumps(data, indent=2)
            st.download_button(
                label="⬇️ Download as JSON",
                data=json_str,
                file_name=f"{selected}_cpq_extract.json",
                mime="application/json",
                use_container_width=True,
            )

            # Build the workbook fresh each time — cheap relative to the
            # Salesforce extraction itself, and avoids caching stale bytes.
            wb = xlsx_export.build_workbook(data)
            xlsx_buffer = io.BytesIO()
            wb.save(xlsx_buffer)
            xlsx_buffer.seek(0)
            st.download_button(
                label="⬇️ Download as Excel (21 tabs)",
                data=xlsx_buffer,
                file_name=f"{selected}_cpq_extract.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )