-- Physical device reservation query (FFF-14).
--
-- Atomically finds and reserves a free physical device for the scheduler.
-- Uses FOR UPDATE SKIP LOCKED so concurrent scheduler runs never deadlock
-- or double-book the same device.
--
-- Eligibility:
--   1. status = 'online' (idle)
--   2. last_seen_at within heartbeat timeout (device is reachable)
--
-- After reservation the device status becomes 'busy'.
-- Returns the device UUID, or NULL when no device is available.
--
-- Usage:
--   SELECT automation.reserve_physical_device();
--   SELECT automation.reserve_physical_device(p_heartbeat_timeout_seconds := 600);

CREATE OR REPLACE FUNCTION automation.reserve_physical_device(
    p_heartbeat_timeout_seconds int DEFAULT 300
) RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
    v_device_id uuid;
BEGIN
    SELECT id INTO v_device_id
    FROM automation.physical_devices
    WHERE status = 'online'
      AND last_seen_at IS NOT NULL
      AND last_seen_at >= now() - make_interval(secs => p_heartbeat_timeout_seconds)
    ORDER BY last_seen_at DESC
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF v_device_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE automation.physical_devices
    SET status = 'busy'
    WHERE id = v_device_id;

    RETURN v_device_id;
END;
$$;
