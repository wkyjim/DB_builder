-- Phase B rollback: reconciliation audit table
--
-- SAFETY: This script will ONLY drop the audit table if it contains NO evidence.
-- If any rows exist, the script fails closed and the table is retained.
--
-- This ensures audit evidence is never casually destroyed during rollback.

DO $$
DECLARE
    row_count INTEGER;
BEGIN
    -- Check if table exists
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'macro_reconciliation_audit'
    ) THEN
        RAISE NOTICE 'Table public.macro_reconciliation_audit does not exist; nothing to drop';
        RETURN;
    END IF;

    -- Check for evidence
    SELECT COUNT(*) INTO row_count FROM public.macro_reconciliation_audit;

    IF row_count > 0 THEN
        RAISE EXCEPTION 'ROLLBACK BLOCKED: public.macro_reconciliation_audit contains % rows. Audit evidence must be retained. Drop the table manually only after evidence has been preserved elsewhere.', row_count;
    END IF;

    -- Safe to drop (no evidence)
    DROP TABLE IF EXISTS public.macro_reconciliation_audit;
    RAISE NOTICE 'Dropped empty public.macro_reconciliation_audit';
END $$;
