# doc_gen/run_document.py
#
# Main CLI and pipeline orchestration for client Word document generation.
# Manages execution flow, dry-run mode, render-only mode, and status updates.

import os
import sys
import json
import argparse
import logging
from datetime import datetime

from doc_gen.facts import compute_facts, load_extraction_data
from doc_gen.packets import build_packets
from doc_gen.llm_client import get_llm_client, DEFAULT_MODEL_ANALYSIS, DEFAULT_MODEL_SYNTHESIS
from doc_gen.analyze import run_all_analyses
from doc_gen.synthesize import synthesize_all
from doc_gen.validate import validate_document_content
from doc_gen.render_docx import render_document

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def update_status(doc_dir: str, state: str, step: str = "", progress: float = 0.0, error: str = None, counts: dict = None):
    """Writes status.json atomically to doc_dir."""
    status_file = os.path.join(doc_dir, "status.json")
    status_data = {
        "state": state,
        "current_step": step,
        "progress": progress,
        "error": error,
        "updated_at": datetime.now().isoformat(),
    }
    if counts:
        status_data["counts"] = counts

    temp_file = status_file + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=2)
    os.replace(temp_file, status_file)


def run_pipeline(run_dir: str, client_name: str = "Client", dry_run: bool = False,
                 mock_llm: bool = False, render_only: bool = False,
                 include_migration_notes: bool = False, progress_callback=None):
    """
    Executes the document generation pipeline.
    """
    run_dir = os.path.abspath(run_dir)
    doc_dir = os.path.join(run_dir, "doc")
    os.makedirs(doc_dir, exist_ok=True)

    def log_progress(msg, progress_val=None, state="running"):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)
        update_status(doc_dir, state=state, step=msg, progress=progress_val or 0.0)

    log_progress("Starting document generation pipeline...", 0.05)

    # 1. Phase A: Compute Facts
    facts_file = os.path.join(doc_dir, "facts.json")
    log_progress("Step 1/5: Computing deterministic catalog facts and observations...", 0.15)
    facts = compute_facts(run_dir)
    with open(facts_file, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)

    # 2. Phase A: Build Packets
    log_progress("Step 2/5: Resolving IDs and building configuration packets...", 0.30)
    packets_result = build_packets(run_dir)
    summary = packets_result.get("summary", {})

    # Save packet files to doc/packets/
    packets_dir = os.path.join(doc_dir, "packets")
    os.makedirs(packets_dir, exist_ok=True)

    for category, pkt_list in [
        ("product_rules", packets_result.get("product_rule_packets", [])),
        ("price_rules", packets_result.get("price_rule_packets", [])),
        ("bundles", packets_result.get("bundle_packets", [])),
        ("standalone", packets_result.get("standalone_packets", [])),
    ]:
        cat_dir = os.path.join(packets_dir, category)
        os.makedirs(cat_dir, exist_ok=True)
        for idx, pkt in enumerate(pkt_list, start=1):
            name = pkt.get("rule_name") or pkt.get("bundle_name") or pkt.get("family") or f"item_{idx}"
            safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)[:40]
            with open(os.path.join(cat_dir, f"{safe_name}.json"), "w", encoding="utf-8") as f:
                json.dump(pkt, f, indent=2)

    # DRY-RUN CHECK
    if dry_run:
        print("\n" + "=" * 60)
        print("DOCUMENT GENERATION DRY-RUN REPORT")
        print("=" * 60)
        print(f"Run Directory:              {run_dir}")
        print(f"Active Products:            {facts.get('active_products'):,}")
        print(f"Configurable Bundles:       {facts.get('total_bundles'):,}")
        print(f"Standalone Products:        {facts.get('standalone_products'):,}")
        print(f"Product Rules:              {facts.get('product_rules', {}).get('total'):,}")
        print(f"Price Rules:                {facts.get('price_rules', {}).get('total'):,}")
        print("-" * 60)
        print("Packet Construction:")
        print(f"  • Product Rule Packets:   {summary.get('product_rule_packets')}")
        print(f"  • Price Rule Packets:     {summary.get('price_rule_packets')}")
        print(f"  • Bundle Packets:         {summary.get('bundle_packets')} (Clustered: {summary.get('clustered_bundle_packets')})")
        print(f"  • Standalone Packets:     {summary.get('standalone_packets')} (Grouped by family)")
        print(f"  Total Packets:            {summary.get('total_packets')}")
        print(f"  Estimated Input Tokens:   ~{summary.get('estimated_total_tokens'):,} tokens")
        print(f"  Estimated LLM Calls:      ~{summary.get('total_packets') + 4} calls (analysis + synthesis)")
        print("-" * 60)
        print("Dry run complete. No network access or LLM calls were made.\n")
        update_status(doc_dir, state="completed", step="Dry run completed successfully.", progress=1.0)
        return summary

    # RENDER-ONLY CHECK
    sections_file = os.path.join(doc_dir, "sections.json")
    docx_file = os.path.join(doc_dir, "client_document.docx")

    if render_only:
        log_progress("Render-only mode: Loading existing sections.json...", 0.60)
        if not os.path.exists(sections_file):
            raise FileNotFoundError(f"Cannot perform --render-only: '{sections_file}' does not exist.")
        with open(sections_file, "r", encoding="utf-8") as f:
            sections = json.load(f)

        raw_data = load_extraction_data(run_dir)
        val_report = validate_document_content(facts, sections, raw_data, run_dir=run_dir)
        if not val_report.get("can_render"):
            raise ValueError(f"Regex check failed! Rendering blocked: {val_report.get('regex_check')}")

        render_document(
            facts=facts,
            sections=sections,
            validation_report=val_report,
            output_filepath=docx_file,
            client_name=client_name,
            include_migration_notes=include_migration_notes,
        )
        log_progress(f"Render-only complete! Saved to {docx_file}", 1.0, state="completed")
        return {"document_path": docx_file, "status": "completed"}

    # 3. Phase B: LLM Analysis
    provider = "mock" if mock_llm else os.environ.get("LLM_PROVIDER", "mock")
    llm_client = get_llm_client(provider)
    log_progress(f"Step 3/5: Running packet analysis (Provider: {provider})...", 0.50)

    analyses = run_all_analyses(
        packets_dict=packets_result,
        run_dir=run_dir,
        client=llm_client,
        model=DEFAULT_MODEL_ANALYSIS,
        progress_callback=lambda msg: update_status(doc_dir, "running", step=msg, progress=0.65),
    )

    # 4. Phase C: Synthesis
    log_progress("Step 4/5: Synthesizing executive summary and section narratives...", 0.80)
    sections = synthesize_all(
        facts=facts,
        analyses_dict=analyses,
        run_dir=run_dir,
        client=llm_client,
        model=DEFAULT_MODEL_SYNTHESIS,
        progress_callback=lambda msg: update_status(doc_dir, "running", step=msg, progress=0.85),
    )

    # 5. Phase C: Validation & Regex Check
    log_progress("Step 5/5: Running validation and regex security checks...", 0.90)
    raw_data = load_extraction_data(run_dir)
    validation_report = validate_document_content(
        facts=facts,
        sections=sections,
        raw_data=raw_data,
        run_dir=run_dir,
    )

    if not validation_report.get("can_render"):
        err_msg = f"Security check failed! Found forbidden Salesforce IDs or SBQQ__ tags: {validation_report['regex_check']['violations']}"
        update_status(doc_dir, state="failed", step=err_msg, error=err_msg)
        raise ValueError(err_msg)

    # 6. Render Word Document
    log_progress(f"Generating Word document for {client_name}...", 0.95)
    render_document(
        facts=facts,
        sections=sections,
        validation_report=validation_report,
        output_filepath=docx_file,
        client_name=client_name,
        include_migration_notes=include_migration_notes,
    )

    log_progress(f"Client document generated successfully! Saved to: {docx_file}", 1.0, state="completed")
    return {
        "status": "completed",
        "document_path": docx_file,
        "facts_path": facts_file,
        "sections_path": sections_file,
        "validation_report": validation_report,
    }


def main():
    parser = argparse.ArgumentParser(description="Salesforce CPQ Client Document Generator")
    parser.add_argument("--run", required=True, help="Path to extraction run directory (e.g. data/runs/sample_org/20261015_150908)")
    parser.add_argument("--client-name", default="Client", help="Client or organization name for document title")
    parser.add_argument("--dry-run", action="store_true", help="Print token/call estimates without invoking LLM or rendering")
    parser.add_argument("--mock-llm", action="store_true", help="Use offline deterministic mock LLM provider")
    parser.add_argument("--render-only", action="store_true", help="Re-render docx from existing sections.json without LLM calls")
    parser.add_argument("--include-migration-notes", action="store_true", help="Include optional section on migration and change-management care areas")

    args = parser.parse_args()

    try:
        run_pipeline(
            run_dir=args.run,
            client_name=args.client_name,
            dry_run=args.dry_run,
            mock_llm=args.mock_llm,
            render_only=args.render_only,
            include_migration_notes=args.include_migration_notes,
        )
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

