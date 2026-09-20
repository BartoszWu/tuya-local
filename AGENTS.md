# Tuya Local

Home Assistant integration. Use `uv run`; runtime/tool settings are in
`pyproject.toml`, checks in `.github/workflows`. Read nested `AGENTS.md` for
device profiles; schema and examples are beside those profiles.

## Start and safety

- Check Git root/remotes/status, especially inside submodules. Preserve user work.
  Fetch origin and upstream for fork work; compare main branches without rewriting
  published history. Use a feature branch and explicit staging for PRs.
- Never publish local keys, device IDs, credentials, private URLs, IP/MAC or raw
  logs. Use sanitized DPS evidence and synthetic fixtures, including in PRs.
- PR work does not authorize merge, live installation, device control or HA restart.

## Validation from repository root

Before every commit:

```sh
uv run ruff check .
uv run ruff check --select I .
uv run ruff format --check .
uv run yamllint custom_components/tuya_local/devices
```

Run `uv run pytest` for integration logic; YAML-only changes:
`uv run pytest tests/test_device_config.py`; translations:
`uv run pytest tests/test_translations.py`. Add tests for new Python behavior,
not just another YAML/JSON profile. Resolve failures; report any checks not run.
Ignore `hacs-validate.yml` when working outside the upstream make-all repository.

## Conventions and PRs

- Reuse generic translation keys/icons; new keys need translations and relevant
  icons. Keep unrelated translation work separate from device/logic PRs.
- Names use sentence case; top-level names are generic, unbranded and omit
  redundant Smart/WiFi. Prefer the appropriate high-level HA entity.
- Device evidence, matching and profile rules are in the nested device guide.
  Do not infer DPS support or writes from a superficially similar device.
- Report changes, checks and limitations; no deployment claims from a commit.
