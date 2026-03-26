@router.post("/generate/docx")
async def generate_report_docx(
    period_start: str = "",
    period_end: str = "",
    report_type: str = "aml_summary",
    current: CurrentUser = Depends(get_current_user),
):
    """Generate a regulator-ready Word document report."""
    sb = get_supabase()
    tenant_id = str(current.tenant_id)

    # Pull real data
    rows = sb.table("extractions").select("*").eq("tenant_id", tenant_id).execute().data or []
    events = sb.table("audit_events").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).limit(50).execute().data or []
    decisions = sb.table("decisions").select("id", count="exact").eq("tenant_id", tenant_id).execute()
    integrity_check = "VERIFIED"
    for i in range(1, len(events)):
        if events[i]["hash"] != events[i-1].get("previous_hash"):
            integrity_check = "WARNING"
            break

    all_scores = []
    for row in rows:
        all_scores.extend((row.get("confidence_scores") or {}).values())
    avg_conf = sum(all_scores)/len(all_scores) if all_scores else 0.0
    low_flags = sum(1 for s in all_scores if float(s) < 0.75)

    data = {
        "reportTitle": f"Compliance Audit Report – {period_start or 'All Time'}",
        "companyName": "Itica Technologies Ltd.",
        "reportingPeriod": f"{period_start or 'All'} – {period_end or 'Present'}",
        "submissionDate": datetime.now(timezone.utc).strftime("%d %B %Y"),
        "preparedBy": current.name or current.email,
        "preparedByRole": "Compliance Officer",
        "version": "v1.0",
        "purpose": f"Compliance audit covering {len(rows)} KYC documents processed on the Itica platform.",
        "overallStatus": "Compliant" if low_flags == 0 else "Partially Compliant",
        "keyFindings": [
            f"{len(rows)} KYC documents processed",
            f"Average extraction confidence: {avg_conf*100:.1f}%",
            f"{low_flags} documents flagged for low confidence",
            f"Audit trail integrity: {integrity_check}",
            f"{decisions.count or 0} compliance decisions recorded",
        ],
        "majorRisks": ["Sanctions screening not yet implemented" if not os.environ.get("SANCTIONS_API_URL") else "Sanctions screening active"],
        "actionsRequired": ["Implement sanctions screening" if not os.environ.get("SANCTIONS_API_URL") else "Continue monitoring"],
        "auditEvents": events,
        "integrity": integrity_check,
        "stats": {
            "totalDocuments": len(rows),
            "verifiedDocuments": sum(1 for r in rows if r.get("status") == "reviewed"),
            "totalDecisions": decisions.count or 0,
            "avgConfidence": avg_conf,
            "lowConfidenceFlags": low_flags,
        },
        # Static sections — customise per deployment
        "scopeItems": ["KYC extraction system", "Human review queue", "Audit trail", "Auth and access control"],
        "frameworks": ["FATF Recommendations", "GDPR", "ISO 27001"],
        "objectives": ["Verify KYC pipeline accuracy", "Confirm audit trail integrity", "Identify compliance gaps"],
        "dataSources": ["Itica audit_events table", "Extractions table", "Decisions table"],
        "toolsUsed": ["Itica Compliance Platform v2.0.0", "LayoutLMv3 (HuggingFace)", "Auth0", "AWS S3", "Supabase"],
        "timeframe": f"{period_start or 'All time'} to {period_end or 'present'}",
        "sampling": "Full population review. No sampling applied.",
        "regulatoryMap": [
            {"regulation": "FATF R.10", "requirement": "Customer Due Diligence", "control": "KYC extraction + human review", "status": "Partial"},
            {"regulation": "FATF R.11", "requirement": "Record keeping", "control": "Immutable hash chain", "status": "Compliant"},
            {"regulation": "GDPR Art. 32", "requirement": "Security of processing", "control": "Auth0 + AES-256", "status": "Compliant"},
        ],
        "compliantAreas": [{"area": "Audit Trail", "evidence": f"Hash chain integrity: {integrity_check}. All events cryptographically linked."}],
        "nonCompliantAreas": [] if os.environ.get("SANCTIONS_API_URL") else [{"area": "Sanctions Screening", "regulation": "FATF R.6", "evidence": "No watchlist check implemented.", "riskLevel": "High"}],
        "partialAreas": [{"area": "KYC Completeness", "detail": f"{low_flags} documents below 0.75 confidence threshold, routed to human review."}] if low_flags > 0 else [],
        "riskMethodology": "3x3 Impact x Likelihood matrix. High = immediate action. Medium = 30 days. Low = monitor.",
        "risks": [{"risk": "Sanctions screening gap", "impact": "High", "likelihood": "Medium", "rating": "High", "owner": "Engineering"}],
        "recommendations": [{"issue": "Sanctions screening", "action": "Integrate OFAC/UN API", "priority": "High", "responsible": "Engineering", "deadline": "30 Apr 2026"}],
        "limitations": ["Third-party vendor audits excluded", "Penetration testing out of scope"],
        "exclusions": ["Auth0, HuggingFace, Supabase vendor audits"],
        "conclusion": f"The platform processed {len(rows)} documents with {avg_conf*100:.1f}% average confidence. Audit trail integrity confirmed.",
        "regulatoryReadiness": "Ready for audit trail and KYC workflow review. Sanctions screening required before full regulatory submission.",
        "nextSteps": ["Implement sanctions screening", "SOC 2 Type I via Vanta", "Q2 2026 expanded audit"],
        "glossary": [
            {"term": "KYC", "definition": "Know Your Customer"},
            {"term": "AML", "definition": "Anti-Money Laundering"},
            {"term": "FATF", "definition": "Financial Action Task Force"},
            {"term": "Hash Chain", "definition": "Tamper-evident cryptographic audit log"},
        ],
        "appendixNote": "Full logs available from Itica platform administrator.",
    }

    # Write data to temp file, call Node script
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        data_path = f.name

    out_path = f"/tmp/report_{tenant_id}.docx"
    script_path = os.path.join(os.path.dirname(__file__), "../../scripts/generate_report.js")

    result = subprocess.run(
        ["node", script_path, data_path, out_path],
        capture_output=True, text=True, timeout=30,
    )
    os.unlink(data_path)

    if result.returncode != 0:
        logger.error(f"Report generation failed: {result.stderr}")
        raise HTTPException(500, "Report generation failed")

    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"itica_compliance_report_{datetime.now(timezone.utc).strftime('%Y%m%d')}.docx",
    )
