-- Core account environment tables for automation schema.
-- Implements one-to-one identity bundle: account → proxy, device_profile, gps_location, app_state.

-- accounts: Instagram accounts used for posting
CREATE TABLE automation.accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL,
    password text NOT NULL,
    recovery_email text,
    platform text NOT NULL DEFAULT 'instagram',
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT accounts_username_platform_uq UNIQUE (username, platform),
    CONSTRAINT accounts_status_chk CHECK (status IN ('active', 'disabled', 'banned', 'suspended')),
    CONSTRAINT accounts_platform_chk CHECK (platform IN ('instagram'))
);

-- proxies: one proxy per account
CREATE TABLE automation.proxies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    host text NOT NULL,
    port integer NOT NULL,
    protocol text NOT NULL DEFAULT 'http',
    username text,
    password text,
    country_code text,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT proxies_status_chk CHECK (status IN ('active', 'inactive', 'expired')),
    CONSTRAINT proxies_protocol_chk CHECK (protocol IN ('http', 'https', 'socks5')),
    CONSTRAINT proxies_port_chk CHECK (port BETWEEN 1 AND 65535)
);

-- device_profiles: device fingerprint per account
CREATE TABLE automation.device_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand text NOT NULL,
    model text NOT NULL,
    android_version text NOT NULL,
    build_fingerprint text,
    user_agent text,
    screen_width integer NOT NULL,
    screen_height integer NOT NULL,
    screen_density integer NOT NULL,
    locale text NOT NULL DEFAULT 'en_US',
    timezone text NOT NULL DEFAULT 'America/New_York',
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT device_profiles_status_chk CHECK (status IN ('active', 'inactive'))
);

-- gps_locations: GPS coordinates for MockGPS
CREATE TABLE automation.gps_locations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label text NOT NULL,
    latitude numeric(10, 7) NOT NULL,
    longitude numeric(10, 7) NOT NULL,
    accuracy_meters numeric(6, 2) NOT NULL DEFAULT 10.0,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT gps_locations_status_chk CHECK (status IN ('active', 'inactive')),
    CONSTRAINT gps_locations_latitude_chk CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT gps_locations_longitude_chk CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT gps_locations_accuracy_chk CHECK (accuracy_meters > 0)
);

-- app_states: Instagram app session/cookies per account
CREATE TABLE automation.app_states (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cookies_json jsonb,
    session_data jsonb,
    instagram_app_version text,
    status text NOT NULL DEFAULT 'active',
    last_synced_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT app_states_status_chk CHECK (status IN ('active', 'expired', 'invalid'))
);

-- account_environments: links account to its identity bundle (all one-to-one)
CREATE TABLE automation.account_environments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES automation.accounts(id),
    proxy_id uuid NOT NULL REFERENCES automation.proxies(id),
    device_profile_id uuid NOT NULL REFERENCES automation.device_profiles(id),
    gps_location_id uuid NOT NULL REFERENCES automation.gps_locations(id),
    app_state_id uuid NOT NULL REFERENCES automation.app_states(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT account_environments_account_uq UNIQUE (account_id),
    CONSTRAINT account_environments_proxy_uq UNIQUE (proxy_id),
    CONSTRAINT account_environments_device_profile_uq UNIQUE (device_profile_id),
    CONSTRAINT account_environments_gps_location_uq UNIQUE (gps_location_id),
    CONSTRAINT account_environments_app_state_uq UNIQUE (app_state_id)
);

-- Indexes for common lookups
CREATE INDEX accounts_status_idx ON automation.accounts (status);
CREATE INDEX proxies_status_idx ON automation.proxies (status);
CREATE INDEX device_profiles_status_idx ON automation.device_profiles (status);
CREATE INDEX gps_locations_status_idx ON automation.gps_locations (status);
CREATE INDEX app_states_status_idx ON automation.app_states (status);

-- updated_at trigger function (reusable across tables)
CREATE OR REPLACE FUNCTION automation.set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply updated_at triggers
CREATE TRIGGER trg_accounts_updated_at
    BEFORE UPDATE ON automation.accounts
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();

CREATE TRIGGER trg_proxies_updated_at
    BEFORE UPDATE ON automation.proxies
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();

CREATE TRIGGER trg_device_profiles_updated_at
    BEFORE UPDATE ON automation.device_profiles
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();

CREATE TRIGGER trg_gps_locations_updated_at
    BEFORE UPDATE ON automation.gps_locations
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();

CREATE TRIGGER trg_app_states_updated_at
    BEFORE UPDATE ON automation.app_states
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();

CREATE TRIGGER trg_account_environments_updated_at
    BEFORE UPDATE ON automation.account_environments
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();
