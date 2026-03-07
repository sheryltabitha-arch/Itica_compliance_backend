"""
Itica — Inference Drift Monitor
Monitors model performance and triggers alerts for degradation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

CONFIDENCE_DROP = 0.05
CORRECTION_RATE_LIMIT = 0.10
UNKNOWN_TYPE_LIMIT = 0.15
TIME_SPIKE = 2.0
PAGE_SIZE = 500


@dataclass
class DriftAlert:
    alert_type: str
    severity: str
    model_version: str
    tenant_id: str | None
    metric_name: str
    current_value: float
    baseline_value: float
    change: float
    detail: str
    triggered_at: str


@dataclass
class DriftReport:
    model_version: str
    tenant_id: str | None
    window_hours: int
    total_inferences: int
    avg_confidence_by_field: dict
    overall_avg_confidence: float
    low_confidence_rate: float
    correction_rate: float
    unknown_doc_type_rate: float
    avg_inference_time_ms: float
    alerts: list
    has_alerts: bool
    computed_at: str


@dataclass
class _WindowStats:
    """Running aggregates built row-by-row."""
    n: int = 0
    low_conf_count: int = 0
    review_count: int = 0
    unknown_type_count: int = 0
    inference_time_sum: float = 0.0
    field_conf: dict[str, list[float]] = field(default_factory=dict)

    def ingest(self, row: dict) -> None:
        self.n += 1
        if row.get("low_confidence_flag"):
            self.low_conf_count += 1
        if row.get("requires_human_review"):
            self.review_count += 1
        if row.get("document_type") == "unknown":
            self.unknown_type_count += 1
        self.inference_time_sum += float(row.get("inference_time_ms") or 0)
        for fn, c in (row.get("confidence_scores") or {}).items():
            entry = self.field_conf.setdefault(fn, [0.0, 0])
            entry[0] += float(c)
            entry[1] += 1

    @property
    def avg_confidence_by_field(self) -> dict[str, float]:
        return {
            fn: vals[0] / vals[1]
            for fn, vals in self.field_conf.items()
            if vals[1] > 0
        }

    @property
    def overall_avg_confidence(self) -> float:
        avgs = self.avg_confidence_by_field
        return sum(avgs.values()) / len(avgs) if avgs else 0.0

    @property
    def low_confidence_rate(self) -> float:
        return self.low_conf_count / self.n if self.n else 0.0

    @property
    def correction_rate(self) -> float:
        return self.review_count / self.n if self.n else 0.0

    @property
    def unknown_doc_type_rate(self) -> float:
        return self.unknown_type_count / self.n if self.n else 0.0

    @property
    def avg_inference_time_ms(self) -> float:
        return self.inference_time_sum / self.n if self.n else 0.0


class DriftMonitor:
    def __init__(self, db):
        self._db = db

    async def compute_report(
        self,
        model_version: str,
        tenant_id: str | None = None,
        window_hours: int = 24,
        baseline_hours: int = 168,
    ) -> DriftReport:
        """Compute drift report comparing window to baseline."""
        now = datetime.now(timezone.utc)
        ws = now - timedelta(hours=window_hours)
        bs = now - timedelta(hours=baseline_hours + window_hours)

        current = await self._stream(model_version, tenant_id, ws, now)
        baseline = await self._stream(model_version, tenant_id, bs, ws)
        alerts: list[DriftAlert] = []

        if current.n == 0:
            return DriftReport(
                model_version=model_version,
                tenant_id=tenant_id,
                window_hours=window_hours,
                total_inferences=0,
                avg_confidence_by_field={},
                overall_avg_confidence=0.0,
                low_confidence_rate=0.0,
                correction_rate=0.0,
                unknown_doc_type_rate=0.0,
                avg_inference_time_ms=0.0,
                alerts=[],
                has_alerts=False,
                computed_at=now.isoformat(),
            )

        cur_avgs = current.avg_confidence_by_field

        if baseline.n > 0:
            base_avgs = baseline.avg_confidence_by_field

            for fn, cur in cur_avgs.items():
                base = base_avgs.get(fn)
                if base is None:
                    continue
                drop = base - cur
                if drop >= CONFIDENCE_DROP:
                    sev = "critical" if drop >= 0.10 else "warning"
                    alerts.append(DriftAlert(
                        alert_type="confidence_drift",
                        severity=sev,
                        model_version=model_version,
                        tenant_id=tenant_id,
                        metric_name=f"confidence:{fn}",
                        current_value=cur,
                        baseline_value=base,
                        change=-drop,
                        detail=f"Field '{fn}' dropped {drop:.1%}",
                        triggered_at=now.isoformat(),
                    ))

        return DriftReport(
            model_version=model_version,
            tenant_id=tenant_id,
            window_hours=window_hours,
            total_inferences=current.n,
            avg_confidence_by_field=cur_avgs,
            overall_avg_confidence=current.overall_avg_confidence,
            low_confidence_rate=current.low_confidence_rate,
            correction_rate=current.correction_rate,
            unknown_doc_type_rate=current.unknown_doc_type_rate,
            avg_inference_time_ms=current.avg_inference_time_ms,
            alerts=alerts,
            has_alerts=bool(alerts),
            computed_at=now.isoformat(),
        )

    async def _stream(
        self,
        model_version: str,
        tenant_id: str | None,
        from_dt: datetime,
        to_dt: datetime,
    ) -> _WindowStats:
        """Stream results in pages, accumulating only aggregates."""
        stats = _WindowStats()
        offset = 0

        while True:
            # TODO: Implement database query
            break

        return stats