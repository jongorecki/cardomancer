#!/usr/bin/env python3
"""
Generate an OpenAPI 3.1 spec by introspecting the Flask app's url_map.

Usage:
    python -m tools.generate_openapi              # writes docs/openapi.yaml
    python -m tools.generate_openapi --json       # also writes docs/openapi.json

What it captures (automatically):
  - Path template, methods, and URL converter types for every @app.route.
  - The view function's first-paragraph docstring as the operation summary
    and the rest as the operation description.
  - A tag derived from the URL prefix (`/api/sort/*` -> "sort",
    `/api/collection/*` -> "collection", etc.).

What it does NOT capture (manual annotation territory):
  - Request body schemas. Most endpoints accept JSON, but the shape is
    only enforced inside the view function; there's no decorator-level
    schema to introspect. We emit a generic `requestBody: {content:
    application/json: {schema: {type: object}}}` for POST/PUT/PATCH so
    consumers know JSON is expected, then defer.
  - Response schemas. Same — we emit a 200 stub with a generic
    object schema.
  - Error responses beyond the global 403 csrf_failed (which DOES apply
    universally to non-safe methods and is documented).

A hand-edited overlay file at `docs/openapi-overlay.yaml` is merged on
top if present. Use that to add real request/response schemas to the
hot-path endpoints without touching the auto-generated skeleton.

Why introspect vs hand-author: the app has ~150 routes. Hand-authoring
them is a project on its own and goes stale within weeks. The
introspected skeleton stays accurate as new routes land; the overlay
captures the parts that demand attention.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unittest.mock as mock
from pathlib import Path

# PyYAML is optional. JSON output works without it; YAML output is
# skipped with a hint to `pip install pyyaml` if it's missing.
try:
    import yaml  # type: ignore
    _HAVE_YAML = True
except ImportError:
    yaml = None  # type: ignore
    _HAVE_YAML = False


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Hardware mocks so we can import web_server without a connected board.
# ---------------------------------------------------------------------------

def _install_hw_mocks():
    """Mock gcode_control + web_camera so importing web_server succeeds
    without a connected machine. The OpenAPI generator only needs the
    url_map; no view function is actually invoked."""
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        gcode_mock.SERIAL_PORT = 'COM3'
        gcode_mock.get_bin_locations = lambda: {}
        gcode_mock._last_serial_error = None
        sys.modules['gcode_control'] = gcode_mock


# ---------------------------------------------------------------------------
# URL → OpenAPI helpers.
# ---------------------------------------------------------------------------

# Flask converter -> OpenAPI primitive type + format
_CONVERTER_TYPES = {
    'string':  ('string',  None),
    'int':     ('integer', 'int32'),
    'float':   ('number',  'float'),
    'uuid':    ('string',  'uuid'),
    'path':    ('string',  None),   # path eats slashes
    'any':     ('string',  None),
}


# Match Flask URL params: <converter:name> or <name>
_PARAM_RE = re.compile(r'<(?:(?P<conv>\w+):)?(?P<name>\w+)>')


def _werkzeug_to_openapi(rule: str) -> str:
    """Translate a Flask URL rule into an OpenAPI path template.

    `/api/sort/configs/<path:filename>` -> `/api/sort/configs/{filename}`
    """
    return _PARAM_RE.sub(lambda m: '{' + m.group('name') + '}', rule)


def _extract_path_params(rule: str) -> list[dict]:
    """Return OpenAPI parameter dicts for each <param> in the rule."""
    out = []
    for m in _PARAM_RE.finditer(rule):
        name = m.group('name')
        conv = m.group('conv') or 'string'
        oas_type, oas_format = _CONVERTER_TYPES.get(conv, ('string', None))
        schema = {'type': oas_type}
        if oas_format:
            schema['format'] = oas_format
        out.append({
            'name':     name,
            'in':       'path',
            'required': True,
            'schema':   schema,
        })
    return out


def _tag_for_path(path: str) -> str:
    """Derive a tag from the URL prefix.

    `/api/sort/configs` -> "sort". The non-/api routes (`/`, `/label`)
    go under "pages". The /api/<word>/... convention is consistent
    enough that this is more useful than a hand-curated mapping.
    """
    if path == '/':
        return 'pages'
    if not path.startswith('/api/'):
        return 'pages'
    parts = path.split('/', 3)  # ['', 'api', 'sort', 'configs/<x>']
    if len(parts) < 3:
        return 'misc'
    return parts[2]


def _docstring_split(doc: str | None) -> tuple[str, str]:
    """Split a docstring into (summary, description) per OpenAPI norms."""
    if not doc:
        return '', ''
    cleaned = doc.strip()
    # First paragraph = summary; rest = description.
    parts = cleaned.split('\n\n', 1)
    summary = parts[0].strip().splitlines()
    # Collapse the summary to one line so it fits the OpenAPI `summary`
    # field cleanly. Tools render multi-line summaries oddly.
    summary_line = ' '.join(s.strip() for s in summary if s.strip())
    description = parts[1].strip() if len(parts) > 1 else ''
    return summary_line, description


def _is_state_changing(methods: set[str]) -> bool:
    return bool(methods & {'POST', 'PUT', 'PATCH', 'DELETE'})


# ---------------------------------------------------------------------------
# Spec assembly.
# ---------------------------------------------------------------------------

def _base_spec() -> dict:
    """The static head of the spec — info, servers, components, tags."""
    return {
        'openapi': '3.1.0',
        'info': {
            'title':       'Cardomancer Card Sorter API',
            'version':     '1.0.0',
            'description': (
                'HTTP API for the Cardomancer Card Sorter. Auto-generated '
                'from `web_server.py` by `tools/generate_openapi.py`. '
                'Most endpoints accept and return JSON. POST/PUT/DELETE/'
                'PATCH endpoints are gated by the CSRF Origin / Referer / '
                'X-Requested-With check (see the `csrf_failed` response '
                'below). Socket.IO traffic is separate and not covered '
                'here.'
            ),
            'contact': {
                'name': 'Cardomancer',
            },
        },
        'servers': [
            {
                'url':         'http://{host}:5000',
                'description': 'Local kiosk server.',
                'variables': {
                    'host': {
                        'default':     'localhost',
                        'description': 'Hostname or LAN IP of the kiosk.',
                    },
                },
            },
        ],
        'components': {
            'schemas': {
                'GenericObject': {
                    'type':                 'object',
                    'additionalProperties': True,
                    'description':          (
                        'Placeholder schema — see route-specific docs or '
                        'inspect the view function. Hand-annotated schemas '
                        'live in docs/openapi-overlay.yaml.'
                    ),
                },
                'ErrorResponse': {
                    'type': 'object',
                    'properties': {
                        'error': {
                            'type':        'string',
                            'description': (
                                'Stable machine-readable code. See '
                                'static/errors.json for the user-facing '
                                'copy mapping.'
                            ),
                        },
                        'message': {
                            'type':        'string',
                            'description': 'Human-readable failure detail.',
                        },
                    },
                    'required': ['error'],
                },
            },
            'responses': {
                'CSRFFailed': {
                    'description': (
                        'Cross-site request blocked. The Origin/Referer '
                        'did not match the kiosk host and the '
                        '`X-Requested-With: XMLHttpRequest` header was '
                        'absent.'
                    ),
                    'content': {
                        'application/json': {
                            'schema': {'$ref': '#/components/schemas/ErrorResponse'},
                            'example': {
                                'error':   'csrf_failed',
                                'message': ('This request was blocked because '
                                            'it did not come from the '
                                            'Cardomancer UI. Reload the page '
                                            'and try again.'),
                            },
                        },
                    },
                },
            },
        },
        'tags': [],  # populated after we know what tags showed up
        'paths': {},
    }


def _build_paths(app) -> tuple[dict, set[str]]:
    """Walk the Flask url_map and assemble a `paths` block.

    Returns (paths_dict, tag_set).
    """
    paths: dict = {}
    tags: set[str] = set()

    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        if rule.endpoint == 'socketio':
            continue
        oas_path = _werkzeug_to_openapi(rule.rule)
        path_params = _extract_path_params(rule.rule)
        view = app.view_functions.get(rule.endpoint)
        summary, description = _docstring_split(
            (view.__doc__ if view else None)
        )
        tag = _tag_for_path(rule.rule)
        tags.add(tag)

        methods = (rule.methods or set()) - {'HEAD', 'OPTIONS'}

        path_block = paths.setdefault(oas_path, {})

        for method in sorted(methods):
            op = {
                'tags':        [tag],
                'operationId': f'{method.lower()}_{rule.endpoint}',
                'summary':     summary or rule.endpoint,
            }
            if description:
                op['description'] = description
            if path_params:
                op['parameters'] = list(path_params)
            if method in ('POST', 'PUT', 'PATCH'):
                op['requestBody'] = {
                    'description': (
                        'JSON body. Hand-edit docs/openapi-overlay.yaml '
                        'to add a precise schema for this endpoint.'
                    ),
                    'content': {
                        'application/json': {
                            'schema': {'$ref': '#/components/schemas/GenericObject'},
                        },
                    },
                    'required': False,
                }
            op['responses'] = {
                '200': {
                    'description': 'Success.',
                    'content': {
                        'application/json': {
                            'schema': {'$ref': '#/components/schemas/GenericObject'},
                        },
                    },
                },
            }
            if _is_state_changing({method}):
                op['responses']['403'] = {
                    '$ref': '#/components/responses/CSRFFailed'
                }
            path_block[method.lower()] = op

    return paths, tags


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive dict merge. Overlay values win at leaves; nested dicts
    merge key-by-key; lists are replaced wholesale (not concatenated)
    because OpenAPI list semantics are heterogeneous (e.g. `tags` on
    an operation is a curated set, not an append-list).

    Used to layer `docs/openapi-overlay.json` on top of the auto-
    generated skeleton so an operator can hand-annotate request /
    response schemas for the endpoints that matter without losing the
    coverage the generator gives.
    """
    out = dict(base)
    for k, v in overlay.items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_overlay() -> dict | None:
    """Return the parsed `docs/openapi-overlay.json` if it exists,
    else None. JSON-only for now (no PyYAML dependency on the overlay
    path)."""
    candidate = _ROOT / 'docs' / 'openapi-overlay.json'
    if not candidate.exists():
        return None
    try:
        with open(candidate, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        # A malformed overlay shouldn't break the live /api/openapi.json
        # endpoint. Skip silently and let the generator output the
        # plain auto-generated skeleton.
        return None


def build_spec(app) -> dict:
    """Build the full OpenAPI doc for the given Flask app.

    Pipeline:
      1. Start from the static `_base_spec` (info, servers, tags,
         components for ErrorResponse / CSRFFailed).
      2. Introspect Flask's url_map to populate `paths`.
      3. Deep-merge `docs/openapi-overlay.json` on top if present —
         that's where hand-annotated request/response schemas live
         for the endpoints that matter most.
    """
    spec = _base_spec()
    paths, tags = _build_paths(app)
    spec['paths'] = paths
    spec['tags'] = [
        {'name': t, 'description': f'Endpoints under /api/{t}/.'}
        for t in sorted(tags) if t != 'pages'
    ] + [
        {'name': 'pages', 'description': 'HTML pages served by the kiosk.'}
        if 'pages' in tags else None
    ]
    spec['tags'] = [t for t in spec['tags'] if t]

    overlay = _load_overlay()
    if overlay:
        spec = _deep_merge(spec, overlay)
    return spec


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/openapi.{json,yaml} from the Flask app."
    )
    parser.add_argument(
        '--no-json', action='store_true',
        help='Skip the JSON output (default: write both).',
    )
    parser.add_argument(
        '--no-yaml', action='store_true',
        help='Skip the YAML output (also skipped automatically if PyYAML '
             'is not installed).',
    )
    parser.add_argument(
        '--out-dir', default='docs',
        help='Output directory relative to repo root (default: docs).',
    )
    args = parser.parse_args(argv)

    _install_hw_mocks()
    import web_server
    spec = build_spec(web_server.app)

    out_dir = _ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    wrote = []
    if not args.no_json:
        json_path = out_dir / 'openapi.json'
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
        wrote.append(str(json_path))

    if not args.no_yaml:
        if _HAVE_YAML:
            yaml_path = out_dir / 'openapi.yaml'
            with open(yaml_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(spec, f, sort_keys=False, allow_unicode=True,
                               default_flow_style=False)
            wrote.append(str(yaml_path))
        else:
            print("PyYAML not installed; skipping YAML output. "
                  "Run `pip install pyyaml` to enable it.",
                  file=sys.stderr)

    for p in wrote:
        print(f"Wrote {p}")

    n_paths = len(spec['paths'])
    n_ops = sum(
        sum(1 for k in v.keys() if k in {'get', 'post', 'put', 'patch', 'delete'})
        for v in spec['paths'].values()
    )
    print(f"  {n_paths} paths, {n_ops} operations, {len(spec['tags'])} tags")
    return 0


if __name__ == '__main__':
    sys.exit(main())
