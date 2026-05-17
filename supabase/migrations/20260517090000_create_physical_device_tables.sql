-- Physical device tables for automation schema.
-- Physical devices are interchangeable executors; one device runs one active job at a time.

-- physical_devices: Android devices connected via Tailscale / ADB TCP
CREATE TABLE automation.physical_devices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alias text NOT NULL,
    adb_serial text,
    genfarmer_device_id text,
    device_name text,
    device_id text,
    os text NOT NULL DEFAULT 'android',
    os_version text,
    tailscale_ipv4 text,
    adb_connect_target text,
    status text NOT NULL DEFAULT 'online',
    last_seen_at timestamptz,
    current_job_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT physical_devices_status_chk CHECK (status IN ('online', 'offline', 'busy', 'maintenance'))
);

-- device_events: audit log for device state changes
CREATE TABLE automation.device_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id uuid NOT NULL REFERENCES automation.physical_devices(id),
    event_type text NOT NULL,
    payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT device_events_event_type_chk CHECK (event_type IN (
        'connected', 'disconnected', 'job_assigned', 'job_released',
        'heartbeat', 'error', 'maintenance_start', 'maintenance_end'
    ))
);

-- Indexes
CREATE INDEX physical_devices_status_idx ON automation.physical_devices (status);
CREATE INDEX physical_devices_adb_serial_idx ON automation.physical_devices (adb_serial) WHERE adb_serial IS NOT NULL;
CREATE INDEX physical_devices_genfarmer_device_id_idx ON automation.physical_devices (genfarmer_device_id) WHERE genfarmer_device_id IS NOT NULL;
CREATE INDEX device_events_device_id_idx ON automation.device_events (device_id);
CREATE INDEX device_events_created_at_idx ON automation.device_events (created_at);

-- updated_at trigger
CREATE TRIGGER trg_physical_devices_updated_at
    BEFORE UPDATE ON automation.physical_devices
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();
