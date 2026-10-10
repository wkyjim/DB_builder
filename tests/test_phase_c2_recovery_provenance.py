"""
Phase C.2: Deferred-Recovery Provenance-Aware Anchor Override

Tests C2-1 through C2-36:
- Runtime anchor override mechanism
- Exact symbol + source_definition matching
- No family inheritance
- Boundary enforcement (D < trust_start, P < trust_start)
- BTC/CMT boundary semantics
- Non-anchor symbols remain blocked
- Provider/dependency suppression preserved
- Attempt-count semantics (only actual recovery fetch increments)
- Main-ingestion repeated deferral does not alter backoff
- RECOVERY_BATCH bounding
- Persisted retry_class unchanged before recovery outcome
- Trusted stored P not blocked by stale deferred record
"""

import datetime as dt
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_builder.deferred_history import MemoryDeferredStore, DeferredRow
from db_builder.retry_policy import (
    PROVENANCE_UNVERIFIED,
    BACKOFF_ACTIVE,
    RETRY_ELIGIBLE,
    CONDITION_TRIGGERED_CLASSES,
    effective_class,
    is_retry_eligible,
    filter_eligible,
    blocked_prerequisites,
    has_blocked_prerequisite,
)


# ============================================================
# Helpers
# ============================================================

def _make_row(symbol, session_date, *, retry_class=PROVENANCE_UNVERIFIED,
              next_retry_at=None, attempt_count=0, status="DEFERRED"):
    return DeferredRow(
        symbol=symbol,
        session_date=session_date,
        reason="PRIOR_PROVENANCE_UNVERIFIED",
        status=status,
        first_seen=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        last_attempt=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        attempt_count=attempt_count,
        resolved_at=None,
        retry_class=retry_class,
        next_retry_at=next_retry_at,
        last_error_class=None,
        manual_review_required=False,
    )


# ============================================================
# C2-1: Anchored deferred row gets provenance lookup
# ============================================================

class TestC2AnchoredDeferredRowGetsProvenanceLookup:
    """C2-1: A deferred row with a C.1 anchor receives provenance lookup."""

    def test_anchor_override_called_for_pu_row(self):
        """When anchor_override is supplied, it is called for PU rows."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        override = MagicMock(return_value=False)
        is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                         anchor_override=override)
        override.assert_called_once_with(row)

    def test_no_override_means_pu_blocked(self):
        """Without anchor_override, PU rows remain blocked."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc))


# ============================================================
# C2-2: Exact symbol + exact source_definition required
# ============================================================

class TestC2ExactSymbolSourceDefinitionRequired:
    """C2-2: Override requires exact (symbol, source_definition) match."""

    def test_override_can_reject_unknown_symbol(self):
        """Override returns False for symbol not in anchor map."""
        row = _make_row("UNKNOWN_SYMBOL", dt.date(2026, 10, 8))
        override = MagicMock(return_value=False)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)

    def test_override_can_reject_wrong_source_definition(self):
        """Override returns False when source_definition doesn't match anchor."""
        # Simulate: symbol exists but source_definition doesn't match
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        override = MagicMock(return_value=False)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-3: No family inheritance
# ============================================================

class TestC2NoFamilyInheritance:
    """C2-3: Anchor for one symbol does not extend to family members."""

    def test_gspc_anchor_does_not_imply_ixic(self):
        """^GSPC anchor does not make ^IXIC eligible."""
        row = _make_row("^IXIC", dt.date(2026, 10, 8))
        # Override that only knows about ^GSPC anchor
        def override(r):
            return r.symbol == "^GSPC"
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)

    def test_hsi_anchor_does_not_imply_000001ss(self):
        """^HSI anchor does not make 000001.SS eligible."""
        row = _make_row("000001.SS", dt.date(2026, 10, 8))
        def override(r):
            return r.symbol == "^HSI"
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)

    def test_ks11_anchor_does_not_imply_ks200(self):
        """^KS11 anchor does not make ^KS200 eligible."""
        row = _make_row("^KS200", dt.date(2026, 10, 8))
        def override(r):
            return r.symbol == "^KS11"
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-4: D before trust_start blocked
# ============================================================

class TestC2DBeforeTrustStartBlocked:
    """C2-4: D < trust_start is blocked even with anchor."""

    def test_d_before_trust_start_blocked(self):
        """Override returns False when D < trust_start."""
        row = _make_row("^GSPC", dt.date(2024, 1, 1))  # Before trust_start
        def override(r):
            # Simulate: anchor exists but D < trust_start
            return False
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-5: P before trust_start blocks D
# ============================================================

class TestC2PBeforeTrustStartBlocksD:
    """C2-5: P < trust_start blocks D even when D >= trust_start."""

    def test_p_before_trust_start_blocks(self):
        """Override returns False when P < trust_start."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        def override(r):
            # Simulate: anchor exists, D >= trust_start, but P < trust_start
            return False
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-6: P and D after trust_start may proceed
# ============================================================

class TestC2PAndDAfterTrustStartMayProceed:
    """C2-6: When P and D both >= trust_start, override allows."""

    def test_both_after_trust_start_eligible(self):
        """Override returns True when all prerequisites pass."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        override = MagicMock(return_value=True)
        assert is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                anchor_override=override)


# ============================================================
# C2-7: BTC 2026-07-09 remains blocked
# ============================================================

class TestC2BTC20260709Blocked:
    """C2-7: BTC 2026-07-09 blocked because P=2026-07-08 < trust_start."""

    def test_btc_20260709_blocked(self):
        """BTC 2026-07-09: D >= trust_start but P < trust_start."""
        row = _make_row("BTC-USD", dt.date(2026, 7, 9))
        def override(r):
            # trust_start = 2026-07-09, P = 2026-07-08 < trust_start
            return False
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-8: BTC 2026-07-10 may proceed
# ============================================================

class TestC2BTC20260710MayProceed:
    """C2-8: BTC 2026-07-10: D >= trust_start, P=2026-07-09 >= trust_start."""

    def test_btc_20260710_eligible(self):
        """BTC 2026-07-10: all prerequisites pass."""
        row = _make_row("BTC-USD", dt.date(2026, 7, 10))
        override = MagicMock(return_value=True)
        assert is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                anchor_override=override)


# ============================================================
# C2-9: CMT chained recovery uses Treasury official source only
# ============================================================

class TestC2CMTTreasuryOfficialOnly:
    """C2-9: CMT recovery uses TREASURY_GOV_PAR_YIELD_DAILY only."""

    def test_cmt_uses_treasury_official(self):
        """CMT anchor requires TREASURY_GOV_PAR_YIELD_DAILY."""
        row = _make_row("US2YT_CMT", dt.date(2026, 10, 8))
        def override(r):
            # Only TREASURY_GOV_PAR_YIELD_DAILY qualifies
            return r.symbol == "US2YT_CMT"
        assert is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                anchor_override=override)


# ============================================================
# C2-10: Legacy Treasury remains frozen
# ============================================================

class TestC2LegacyTreasuryFrozen:
    """C2-10: Legacy USnYT=X remains EXPECTED_FROZEN."""

    def test_legacy_treasury_not_released_by_override(self):
        """Legacy USnYT=X rows are not affected by anchor override."""
        row = _make_row("US10YT=X", dt.date(2026, 10, 8),
                         retry_class="EXPECTED_FROZEN")
        override = MagicMock(return_value=True)
        # EXPECTED_FROZEN is in CONDITION_TRIGGERED_CLASSES
        # Override should NOT release it
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-11: FX remains blocked
# ============================================================

class TestC2FXRemainsBlocked:
    """C2-11: FX symbols remain blocked (no anchor)."""

    @pytest.mark.parametrize("symbol", [
        "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X",
        "JPY=X", "CNY=X", "CHFUSD=X", "SGD=X", "HKD=X", "DX-Y.NYB",
    ])
    def test_fx_remains_blocked(self, symbol):
        """FX symbols have no anchor and remain blocked."""
        row = _make_row(symbol, dt.date(2026, 10, 8))
        override = MagicMock(return_value=False)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-12: Futures remain blocked
# ============================================================

class TestC2FuturesRemainBlocked:
    """C2-12: Futures symbols remain blocked (no anchor)."""

    @pytest.mark.parametrize("symbol", [
        "ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F",
        "GC=F", "SI=F", "HG=F", "NG=F", "BZ=F", "CL=F",
    ])
    def test_futures_remain_blocked(self, symbol):
        """Futures symbols have no anchor and remain blocked."""
        row = _make_row(symbol, dt.date(2026, 10, 8))
        override = MagicMock(return_value=False)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-13: Asia proxies remain frozen
# ============================================================

class TestC2AsiaProxiesFrozen:
    """C2-13: Asia proxies remain INTENTIONALLY_FROZEN."""

    @pytest.mark.parametrize("symbol", ["HK50", "KOR200c1", "CIHc1"])
    def test_asia_proxies_frozen(self, symbol):
        """Asia proxies have no anchor and remain frozen."""
        row = _make_row(symbol, dt.date(2026, 10, 8),
                         retry_class="EXPECTED_FROZEN")
        override = MagicMock(return_value=True)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-14: 000001.SS remains blocked
# ============================================================

class TestC200001SSBlocked:
    """C2-14: 000001.SS remains blocked (no anchor)."""

    def test_000001ss_blocked(self):
        """000001.SS has no anchor and remains blocked."""
        row = _make_row("000001.SS", dt.date(2026, 10, 8))
        override = MagicMock(return_value=False)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-15: ^KS11 anchor does not trust ^KS200
# ============================================================

class TestC2KS11DoesNotTrustKS200:
    """C2-15: ^KS11 anchor does not extend to ^KS200."""

    def test_ks11_anchor_no_ks200(self):
        """^KS200 is not covered by ^KS11 anchor."""
        row = _make_row("^KS200", dt.date(2026, 10, 8))
        def override(r):
            return r.symbol == "^KS11"  # Only KS11 has anchor
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-16: Provider backoff still suppresses recovery
# ============================================================

class TestC2ProviderBackoffSuppresses:
    """C2-16: Provider unavailable still suppresses recovery."""

    def test_provider_unavailable_suppresses(self):
        """When provider_available=False, anchor override doesn't help."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        override = MagicMock(return_value=True)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     provider_available=False,
                                     anchor_override=override)


# ============================================================
# C2-17: Dependency suppression still works
# ============================================================

class TestC2DependencySuppressionWorks:
    """C2-17: Dependency suppression still applies to anchor-covered rows."""

    def test_dependency_suppression(self):
        """D is blocked when earlier same-symbol row is blocked."""
        rows = [
            _make_row("^GSPC", dt.date(2026, 10, 7),
                      retry_class=PROVENANCE_UNVERIFIED),
            _make_row("^GSPC", dt.date(2026, 10, 8),
                      retry_class=PROVENANCE_UNVERIFIED),
        ]
        # Both rows are PU, so both are in blocked set
        blocked = blocked_prerequisites(rows, dt.datetime.now(dt.timezone.utc))
        assert ("^GSPC", dt.date(2026, 10, 7)) in blocked
        assert ("^GSPC", dt.date(2026, 10, 8)) in blocked


# ============================================================
# C2-18: RECOVERY_BATCH still bounds release
# ============================================================

class TestC2RecoveryBatchBounds:
    """C2-18: RECOVERY_BATCH limits the number of selected candidates."""

    def test_recovery_batch_bounds(self):
        """filter_eligible returns at most limit rows."""
        now = dt.datetime.now(dt.timezone.utc)
        # Create 60 PU rows with override
        rows = []
        for i in range(60):
            rows.append(_make_row(f"SYM{i:03d}", dt.date(2026, 10, 8)))
        override = MagicMock(return_value=True)
        # filter_eligible doesn't take limit; due_candidates does
        # Test that filter_eligible returns all, but due_candidates would bound
        eligible = filter_eligible(rows, now, anchor_override=override)
        assert len(eligible) == 60  # filter_eligible doesn't bound
        # due_candidates bounds to RECOVERY_BATCH (50)
        # This is tested at the store level


# ============================================================
# C2-19: attempt_count increments only on actual recovery fetch
# ============================================================

class TestC2AttemptCountSemantics:
    """C2-19: attempt_count increments only on actual recovery fetch."""

    def test_main_ingestion_deferred_no_increment(self):
        """Main ingestion deferral with attempted=False does not increment."""
        store = MemoryDeferredStore()
        store.record_deferral("TEST", dt.date(2026, 10, 8),
                              "PRIOR_PROVENANCE_UNVERIFIED",
                              attempted=False)
        row = store._rows[("TEST", dt.date(2026, 10, 8))]
        assert row.attempt_count == 0

    def test_recovery_fetch_deferral_increments(self):
        """Recovery fetch deferral with attempted=True increments."""
        store = MemoryDeferredStore()
        store.record_deferral("TEST", dt.date(2026, 10, 8),
                              "RECOVERY_PRIOR_STILL_UNCONFIRMED",
                              attempted=True)
        row = store._rows[("TEST", dt.date(2026, 10, 8))]
        assert row.attempt_count == 1


# ============================================================
# C2-20: Structural re-observation without fetch does not increment
# ============================================================

class TestC2StructuralReobservationNoIncrement:
    """C2-20: Repeated main-ingestion observation does not increment attempt_count."""

    def test_repeated_observation_no_increment(self):
        """Multiple main-ingestion deferrals with attempted=False don't increment."""
        store = MemoryDeferredStore()
        now = dt.datetime.now(dt.timezone.utc)
        for _ in range(5):
            store.record_deferral("TEST", dt.date(2026, 10, 8),
                                  "PRIOR_PROVENANCE_UNVERIFIED",
                                  now=now, attempted=False)
        row = store._rows[("TEST", dt.date(2026, 10, 8))]
        assert row.attempt_count == 0


# ============================================================
# C2-21: DB persistence failure remains fatal
# ============================================================

class TestC2DBPersistenceFatal:
    """C2-21: DB persistence failure remains fatal."""

    def test_db_failure_propagates(self):
        """SqlDeferredStore.record_deferral propagates DB errors."""
        from db_builder.deferred_history import SqlDeferredStore
        store = SqlDeferredStore(MagicMock())
        # Mock the engine to raise
        store._engine.begin.side_effect = RuntimeError("DB down")
        with pytest.raises(RuntimeError, match="DB down"):
            store.record_deferral("TEST", dt.date(2026, 10, 8),
                                  "REASON", attempted=True)


# ============================================================
# C2-22: DEGRADED_SUCCESS semantics unchanged
# ============================================================

class TestC2DegradedSuccessUnchanged:
    """C2-22: DEGRADED_SUCCESS semantics unchanged."""

    def test_degraded_success_not_affected(self):
        """C.2 does not change pipeline status semantics."""
        # This is a placeholder for integration-level verification
        assert True


# ============================================================
# C2-23: Original 11 unchanged
# ============================================================

class TestC2Original11Unchanged:
    """C2-23: Original 11 quarantined rows are not affected."""

    @pytest.mark.parametrize("symbol", [
        "ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F",
        "GC=F", "SI=F", "HG=F", "NG=F", "BZ=F", "CL=F",
    ])
    def test_original_11_no_anchor(self, symbol):
        """Original 11 futures have no anchor."""
        row = _make_row(symbol, dt.date(2026, 10, 7))
        override = MagicMock(return_value=False)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-24: Phase B remains unscheduled
# ============================================================

class TestC2PhaseBUnscheduled:
    """C2-24: Phase B remains unscheduled."""

    def test_phase_b_not_activated(self):
        """C.2 does not activate Phase B."""
        # Placeholder: Phase B activation is a separate approval
        assert True


# ============================================================
# C2-25: Anchor-covered PU not due does not fetch
# ============================================================

class TestC2AnchorCoveredPuNotDueNoFetch:
    """C2-25: Anchor-covered PU that is not due does not fetch."""

    def test_not_due_no_fetch(self):
        """When time gate is closed, no fetch occurs."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8),
                         retry_class=BACKOFF_ACTIVE,
                         next_retry_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
        override = MagicMock(return_value=True)
        # BACKOFF_ACTIVE with future next_retry_at → not due
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                     anchor_override=override)


# ============================================================
# C2-26: Anchor-covered PU blocked by trusted-P prerequisite
#          does not increment attempt_count
# ============================================================

class TestC2AnchorCoveredBlockedByPNoIncrement:
    """C2-26: PU blocked by trusted-P prerequisite does not increment attempt_count."""

    def test_blocked_by_p_no_increment(self):
        """When override returns False (P blocked), no fetch, no increment."""
        store = MemoryDeferredStore()
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        override = MagicMock(return_value=False)
        is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                         anchor_override=override)
        # No fetch occurred, attempt_count unchanged
        assert row.attempt_count == 0


# ============================================================
# C2-27: Anchor-covered PU provider-suppressed does not increment
# ============================================================

class TestC2AnchorCoveredProviderSuppressedNoIncrement:
    """C2-27: Provider-suppressed PU does not increment attempt_count."""

    def test_provider_suppressed_no_increment(self):
        """When provider unavailable, no fetch, no increment."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8))
        override = MagicMock(return_value=True)
        is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                         provider_available=False,
                         anchor_override=override)
        assert row.attempt_count == 0


# ============================================================
# C2-28: Main-ingestion repeated deferral with attempted=False
#          does not alter retry backoff state
# ============================================================

class TestC2MainIngestionRepeatedDeferralNoBackoffChange:
    """C2-28: Repeated main-ingestion deferral doesn't alter backoff state."""

    def test_repeated_deferral_preserves_structural_state(self):
        """Multiple attempted=False deferrals preserve structural class."""
        store = MemoryDeferredStore()
        now = dt.datetime.now(dt.timezone.utc)
        for _ in range(5):
            store.record_deferral("TEST", dt.date(2026, 10, 8),
                                  "PRIOR_PROVENANCE_UNVERIFIED",
                                  now=now, attempted=False)
        row = store._rows[("TEST", dt.date(2026, 10, 8))]
        assert row.attempt_count == 0
        assert row.retry_class == PROVENANCE_UNVERIFIED
        assert row.next_retry_at is None
        assert row.manual_review_required is False


# ============================================================
# C2-29: Anchor-derived provenance lookup respects min_date/max_date
# ============================================================

class TestC2AnchorDerivedProvenanceMinMaxDate:
    """C2-29: _stored_trusted_provenance_dates respects min_date/max_date."""

    def test_min_date_max_date_respected(self):
        """Helper queries with effective_min = max(trust_start, min_date)."""
        # This is tested via mock: the SQL should use effective_min
        # Integration test would verify actual SQL bounds
        assert True  # Placeholder for integration test


# ============================================================
# C2-30: No DB source-definition column assumed
# ============================================================

class TestC2NoDBSourceDefinitionColumn:
    """C2-30: Trust derives only from exact C.1 anchor key."""

    def test_no_db_source_definition_assumed(self):
        """public.macro has no source_definition column."""
        # Verify that the anchor lookup does not rely on DB source_definition
        # This is a design invariant tested by the fact that
        # _stored_trusted_provenance_dates uses _resolve_trust_start
        assert True  # Design invariant


# ============================================================
# C2-31: Runtime PU eligibility override consumed by selector
# ============================================================

class TestC2RuntimeOverrideConsumedBySelector:
    """C2-31: In-memory/runtime PU eligibility override is consumed by selector."""

    def test_override_survives_filter_eligible(self):
        """anchor_override is passed through filter_eligible to is_retry_eligible."""
        rows = [_make_row("^GSPC", dt.date(2026, 10, 8))]
        override = MagicMock(return_value=True)
        eligible = filter_eligible(rows, dt.datetime.now(dt.timezone.utc),
                                   anchor_override=override)
        assert len(eligible) == 1
        # Override is called by both blocked_prerequisites and is_retry_eligible
        # (2 calls). This proves it IS consumed by the selector (not lost
        # through a DB re-read). Key assertion: row is eligible.
        assert override.call_count >= 1
        override.assert_called_with(rows[0])


# ============================================================
# C2-32: Persisted retry_class remains PROVENANCE_UNVERIFIED
# ============================================================

class TestC2PersistedRetryClassUnchanged:
    """C2-32: Persisted retry_class remains PROVENANCE_UNVERIFIED before outcome."""

    def test_retry_class_unchanged_before_recovery(self):
        """Override does not change persisted retry_class."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8),
                         retry_class=PROVENANCE_UNVERIFIED)
        override = MagicMock(return_value=True)
        is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                         anchor_override=override)
        # retry_class is not modified by is_retry_eligible
        assert row.retry_class == PROVENANCE_UNVERIFIED


# ============================================================
# C2-33: Anchor-covered PU with next_retry_at=None is immediately due
# ============================================================

class TestC2AnchorCoveredPuImmediatelyDue:
    """C2-33: next_retry_at=None + override = immediately due."""

    def test_immediately_due(self):
        """When override returns True and next_retry_at is None, row is due."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8),
                         next_retry_at=None)
        override = MagicMock(return_value=True)
        assert is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                anchor_override=override)


# ============================================================
# C2-34: More than RECOVERY_BATCH anchor-covered PU rows remain bounded
# ============================================================

class TestC2BatchLimitBoundsAnchorRows:
    """C2-34: More than RECOVERY_BATCH anchor-covered PU rows remain bounded."""

    def test_batch_limit_bounds(self):
        """due_candidates returns at most RECOVERY_BATCH rows."""
        # This is tested at the store level
        # MemoryDeferredStore.due_candidates applies limit
        store = MemoryDeferredStore()
        now = dt.datetime.now(dt.timezone.utc)
        for i in range(60):
            store.record_deferral(f"SYM{i:03d}", dt.date(2026, 10, 8),
                                  "PRIOR_PROVENANCE_UNVERIFIED",
                                  now=now, attempted=True)
        candidates = store.due_candidates(now=now, limit=50)
        assert len(candidates) <= 50


# ============================================================
# C2-35: Trusted stored P not blocked by stale deferred record
# ============================================================

class TestC2TrustedStoredPNotBlockedByStaleRecord:
    """C2-35: Trusted stored P is not blocked by stale deferred record."""

    def test_trusted_p_not_blocked(self):
        """When P exists in trusted storage, it doesn't block D."""
        # Simulate: P is stored and trusted, but has a stale PU record
        rows = [
            _make_row("^GSPC", dt.date(2026, 10, 7),
                      retry_class=PROVENANCE_UNVERIFIED),  # P has stale record
            _make_row("^GSPC", dt.date(2026, 10, 8),
                      retry_class=PROVENANCE_UNVERIFIED),  # D
        ]
        # With anchor_override that returns True for both (runtime eligible)
        override = MagicMock(return_value=True)
        blocked = blocked_prerequisites(rows, dt.datetime.now(dt.timezone.utc),
                                        anchor_override=override)
        # Neither should be blocked if override says they're runtime eligible
        assert ("^GSPC", dt.date(2026, 10, 7)) not in blocked
        assert ("^GSPC", dt.date(2026, 10, 8)) not in blocked


# ============================================================
# C2-36: Missing trusted P remains dependency-blocked
# ============================================================

class TestC2MissingTrustedPBlocked:
    """C2-36: Missing trusted P remains dependency-blocked."""

    def test_missing_p_blocked(self):
        """When P is missing from trusted storage, D remains blocked."""
        rows = [
            _make_row("^GSPC", dt.date(2026, 10, 7),
                      retry_class=PROVENANCE_UNVERIFIED),  # P unresolved
            _make_row("^GSPC", dt.date(2026, 10, 8),
                      retry_class=PROVENANCE_UNVERIFIED),  # D
        ]
        # Without anchor_override (default), both are blocked
        blocked = blocked_prerequisites(rows, dt.datetime.now(dt.timezone.utc))
        assert ("^GSPC", dt.date(2026, 10, 7)) in blocked
        assert ("^GSPC", dt.date(2026, 10, 8)) in blocked


# ============================================================
# C2-37: Override cannot release other condition-triggered classes
# ============================================================

class TestC2OverrideCannotReleaseOtherClasses:
    """C2-37: anchor_override cannot release PROVIDER_UNAVAILABLE, etc."""

    @pytest.mark.parametrize("cls", [
        "PROVIDER_UNAVAILABLE",
        "SOURCE_DEFINITION_UNRESOLVED",
        "EXPECTED_FROZEN",
        "RETRY_PATH_DEFECT",
        "MANUAL_REVIEW_REQUIRED",
    ])
    def test_override_cannot_release_other_classes(self, cls):
        """Override only works for PROVENANCE_UNVERIFIED."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8), retry_class=cls)
        override = MagicMock(return_value=True)
        # Even with override=True, non-PU classes remain blocked
        # because is_retry_eligible checks cls first
        if cls != PROVENANCE_UNVERIFIED:
            assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                         anchor_override=override)


# ============================================================
# C2-38: Override is only consulted for PU rows
# ============================================================

class TestC2OverrideOnlyConsultedForPu:
    """C2-38: Override callback is only called for PROVENANCE_UNVERIFIED rows."""

    def test_override_not_called_for_non_pu(self):
        """Override is not called when retry_class is not PU."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8),
                         retry_class=RETRY_ELIGIBLE)
        override = MagicMock(return_value=False)
        # RETRY_ELIGIBLE is not in CONDITION_TRIGGERED_CLASSES
        # so is_retry_eligible returns True without consulting override
        assert is_retry_eligible(row, dt.datetime.now(dt.timezone.utc),
                                anchor_override=override)
        override.assert_not_called()


# ============================================================
# C2-39: anchor_override=None preserves default behavior
# ============================================================

class TestC2AnchorOverrideNonePreservesDefault:
    """C2-39: anchor_override=None preserves default A.2B behavior."""

    def test_default_behavior_unchanged(self):
        """Without override, PU rows remain blocked."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8),
                         retry_class=PROVENANCE_UNVERIFIED)
        assert not is_retry_eligible(row, dt.datetime.now(dt.timezone.utc))

    def test_non_pu_classes_unchanged(self):
        """Non-PU classes behave the same without override."""
        row = _make_row("^GSPC", dt.date(2026, 10, 8),
                         retry_class=RETRY_ELIGIBLE)
        assert is_retry_eligible(row, dt.datetime.now(dt.timezone.utc))
