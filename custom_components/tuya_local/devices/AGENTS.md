# Device profiles

Read [README](README.md) and validate against `device_config_schema.json`.
Use existing profiles as examples; the schema is authoritative for fields/types.

- Obtain sanitized LOCAL DPS evidence; any DPS absent there must be optional.
  For product ID/model details use the Tuya developer platform's Query Device
  Details and Query Things Data Model. Never publish the account/device ID.
  Resolve uncertain DP meanings with the user rather than guessing.
- Search for an existing functional match (`uv run best_match` from repo root).
  A product-only difference belongs in that profile's products, not a duplicate.
  For a close match prefer a recent profile; additions to existing variants must
  be optional so older devices continue to match.
- Filenames: brand_model_type, lowercase ASCII alphanumerics with underscores
  separating parts; omit model if unknown. Check collisions before adding v2/v3
  to the model. Branding belongs in products, not the generic top-level name.
- Products require a confirmed product ID; add manufacturer/model when known.
- Prefer high-level entities: climate for heaters/AC/heat pumps, water_heater
  for hot water, humidifier for humidifiers/dehumidifiers, fan for air purifiers.
- Reuse translation keys. Primary functions/ordinary sensors have no category;
  infrequent settings use config, troubleshooting sensors diagnostic.
- Child lock: lock with child_lock translation key. Bitmap faults: problem
  binary_sensor, zero=false/default=true, raw fault_code attribute; add human
  descriptions only with evidence. Check nearby profiles for mapping syntax.
- A sole sensor of a class normally needs no name; multiples use available
  class_x translation keys/placeholders or unique names. A clear primary may
  remain unnamed. Prefer existing translation keys over explicit names/icons.
- For a new profile run `uv run duplicates <config-filename>` from repo root;
  explain any 100% matches in the PR. Root instructions define lint/test commands.
