"""
Tests for the auto-generated OpenAPI spec.

These exercise the introspection generator in `tools/generate_openapi.py`
and the live `/api/openapi.json` Flask route. The point isn't to pin every
field — that would burn out the moment we add a route — but to confirm:

  1. The generator produces a syntactically valid OpenAPI 3.1 skeleton.
  2. Every route in the Flask url_map shows up in the spec (no silent
     drift between the running app and the documented API).
  3. Path params translate from Flask's `<int:foo>` to OpenAPI's
     `{foo}` with the correct schema type.
  4. State-changing methods document the CSRF 403 response.
  5. The live /api/openapi.json endpoint returns the same shape.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import web_server
from tools.generate_openapi import (
    build_spec,
    _werkzeug_to_openapi,
    _extract_path_params,
    _tag_for_path,
    _docstring_split,
)


class WerkzeugToOpenAPITests(unittest.TestCase):
    """Path-template translation."""

    def test_no_params_passthrough(self):
        self.assertEqual(_werkzeug_to_openapi('/api/status'), '/api/status')

    def test_string_param(self):
        self.assertEqual(
            _werkzeug_to_openapi('/api/sort/configs/<filename>'),
            '/api/sort/configs/{filename}',
        )

    def test_int_param(self):
        self.assertEqual(
            _werkzeug_to_openapi('/api/bins/test/<int:bin_number>'),
            '/api/bins/test/{bin_number}',
        )

    def test_path_param(self):
        self.assertEqual(
            _werkzeug_to_openapi('/api/sort/configs/<path:filename>'),
            '/api/sort/configs/{filename}',
        )

    def test_multiple_params(self):
        self.assertEqual(
            _werkzeug_to_openapi(
                '/api/review/scan/<session_name>/<int:scan_num>'),
            '/api/review/scan/{session_name}/{scan_num}',
        )


class ExtractPathParamsTests(unittest.TestCase):
    """Converter type → OpenAPI schema."""

    def test_default_string(self):
        params = _extract_path_params('/foo/<bar>')
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0]['name'], 'bar')
        self.assertEqual(params[0]['schema'], {'type': 'string'})

    def test_int_gets_int32_format(self):
        params = _extract_path_params('/x/<int:n>')
        self.assertEqual(params[0]['schema'],
                         {'type': 'integer', 'format': 'int32'})

    def test_path_converter_stays_string(self):
        params = _extract_path_params('/x/<path:p>')
        self.assertEqual(params[0]['schema'], {'type': 'string'})

    def test_multiple(self):
        params = _extract_path_params('/a/<x>/b/<int:y>')
        self.assertEqual([p['name'] for p in params], ['x', 'y'])
        self.assertEqual(params[0]['in'], 'path')
        self.assertTrue(all(p['required'] for p in params))


class TagDerivationTests(unittest.TestCase):
    """Tag-from-URL-prefix mapping."""

    def test_root(self):
        self.assertEqual(_tag_for_path('/'), 'pages')

    def test_non_api_route(self):
        self.assertEqual(_tag_for_path('/label'), 'pages')

    def test_api_sort(self):
        self.assertEqual(_tag_for_path('/api/sort/configs'), 'sort')

    def test_api_collection_locate(self):
        self.assertEqual(_tag_for_path('/api/collection/locate'), 'collection')

    def test_api_short(self):
        self.assertEqual(_tag_for_path('/api/status'), 'status')


class DocstringSplitTests(unittest.TestCase):
    """Summary / description extraction."""

    def test_none(self):
        s, d = _docstring_split(None)
        self.assertEqual(s, '')
        self.assertEqual(d, '')

    def test_summary_only(self):
        s, d = _docstring_split("Return foo bar.")
        self.assertEqual(s, 'Return foo bar.')
        self.assertEqual(d, '')

    def test_summary_and_description(self):
        s, d = _docstring_split("Short summary.\n\nLong description body.")
        self.assertEqual(s, 'Short summary.')
        self.assertEqual(d, 'Long description body.')

    def test_multi_line_summary_collapsed(self):
        """Multi-line summary paragraph should collapse to one line."""
        s, _d = _docstring_split("Line one\nline two.\n\nDescription.")
        self.assertIn('Line one', s)
        self.assertIn('line two', s)
        self.assertNotIn('\n', s)


class BuildSpecTests(unittest.TestCase):
    """The full spec generated against the running app."""

    @classmethod
    def setUpClass(cls):
        cls.spec = build_spec(web_server.app)

    def test_basic_shape(self):
        self.assertEqual(self.spec['openapi'], '3.1.0')
        self.assertEqual(self.spec['info']['title'],
                         'Cardomancer Card Sorter API')
        self.assertIn('paths', self.spec)
        self.assertIn('components', self.spec)

    def test_has_csrf_response_component(self):
        self.assertIn('CSRFFailed',
                      self.spec['components']['responses'])

    def test_every_url_map_rule_appears(self):
        """No route is dropped silently between the Flask app and the
        spec. Static + Socket.IO are intentionally excluded."""
        spec_paths = set(self.spec['paths'].keys())
        rule_paths = set()
        for rule in web_server.app.url_map.iter_rules():
            if rule.endpoint in ('static', 'socketio'):
                continue
            rule_paths.add(_werkzeug_to_openapi(rule.rule))
        missing = rule_paths - spec_paths
        self.assertFalse(
            missing,
            f"OpenAPI spec missing {len(missing)} routes from the live "
            f"app: {sorted(missing)[:10]}...",
        )

    def test_state_changing_methods_get_csrf_response(self):
        """Every POST/PUT/PATCH/DELETE operation references the 403
        CSRFFailed response."""
        offenders = []
        for path, ops in self.spec['paths'].items():
            for method, op in ops.items():
                if method.upper() in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                    resp = op.get('responses', {})
                    has_403 = '403' in resp
                    if not has_403:
                        offenders.append(f"{method.upper()} {path}")
        self.assertEqual(offenders, [],
                         f"State-changing ops without 403: {offenders[:5]}")

    def test_post_endpoints_declare_json_request_body(self):
        """POST/PUT/PATCH operations document the JSON body shape."""
        for path, ops in self.spec['paths'].items():
            for method in ('post', 'put', 'patch'):
                op = ops.get(method)
                if op is None:
                    continue
                # The body is optional (some POSTs are param-free), but
                # the schema declaration should still be present.
                self.assertIn(
                    'requestBody', op,
                    f"{method.upper()} {path} missing requestBody declaration",
                )

    def test_known_route_is_present(self):
        """Spot-check: /api/status (GET) is documented."""
        self.assertIn('/api/status', self.spec['paths'])
        self.assertIn('get', self.spec['paths']['/api/status'])

    def test_path_param_route_translated(self):
        """Spot-check: /api/bins/test/<int:bin_number> shows up as
        /api/bins/test/{bin_number} with integer schema."""
        path = '/api/bins/test/{bin_number}'
        self.assertIn(path, self.spec['paths'])
        post = self.spec['paths'][path].get('post')
        self.assertIsNotNone(post)
        params = {p['name']: p for p in post.get('parameters', [])}
        self.assertIn('bin_number', params)
        self.assertEqual(params['bin_number']['schema']['type'], 'integer')


class LiveOpenAPIRouteTests(unittest.TestCase):
    """End-to-end test against the /api/openapi.json route."""

    def setUp(self):
        self.client = web_server.app.test_client()

    def test_endpoint_returns_200_json(self):
        r = self.client.get('/api/openapi.json')
        self.assertEqual(r.status_code, 200)
        # Flask jsonify sets application/json
        self.assertTrue(r.content_type.startswith('application/json'))
        data = r.get_json()
        self.assertEqual(data['openapi'], '3.1.0')

    def test_endpoint_matches_generator(self):
        """The live endpoint and the generator agree on path count."""
        r = self.client.get('/api/openapi.json')
        live = r.get_json()
        offline = build_spec(web_server.app)
        self.assertEqual(set(live['paths'].keys()),
                         set(offline['paths'].keys()))

    def test_docs_page_renders(self):
        r = self.client.get('/docs')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('swagger-ui', body)
        self.assertIn('/api/openapi.json', body)


class OverlayMergeTests(unittest.TestCase):
    """Pin the `docs/openapi-overlay.json` -> spec merge contract.

    The overlay layers hand-annotated request/response schemas on top
    of the auto-generated skeleton. We assert:
      1. The overlay's schema components show up in the merged spec.
      2. A few representative paths got the precise schema instead of
         the GenericObject placeholder.
      3. Deep-merge preserves the auto-generated fields the overlay
         didn't override (e.g. tags, operationId).
    """

    @classmethod
    def setUpClass(cls):
        from tools.generate_openapi import build_spec, _deep_merge
        cls.build_spec = staticmethod(build_spec)
        cls._deep_merge = staticmethod(_deep_merge)
        cls.spec = build_spec(web_server.app)

    def test_overlay_schema_components_present(self):
        """A representative subset of the overlay's named schemas
        survives into the final spec under components.schemas."""
        schemas = self.spec['components']['schemas']
        for name in (
            'WorkerState', 'SessionStartRequest', 'StatusResponse',
            'CollectionStats', 'InventoryRow', 'CollectionFilterResponse',
            'EnrichmentSource', 'DatabaseStatusResponse',
        ):
            self.assertIn(name, schemas,
                          f"Overlay schema {name!r} missing from spec")

    def test_session_start_has_typed_request_body(self):
        """POST /api/session/start should reference SessionStartRequest
        instead of the generator's GenericObject."""
        body = (self.spec['paths']['/api/session/start']['post']
                ['requestBody']['content']['application/json']['schema'])
        self.assertEqual(body, {'$ref': '#/components/schemas/SessionStartRequest'})

    def test_collection_filter_has_typed_response_and_params(self):
        """GET /api/collection/filter should have a CollectionFilterResponse
        schema AND query parameters from the overlay (q, page, per_page)."""
        op = self.spec['paths']['/api/collection/filter']['get']
        ok = op['responses']['200']['content']['application/json']['schema']
        self.assertEqual(ok, {'$ref': '#/components/schemas/CollectionFilterResponse'})
        names = {p['name'] for p in op.get('parameters', [])}
        for required in ('q', 'page', 'per_page'):
            self.assertIn(required, names,
                          f"Overlay should add `{required}` query param")

    def test_overlay_preserves_auto_generated_fields(self):
        """Deep merge must NOT clobber operationId / tags that the
        auto-generator put in place when the overlay didn't override
        those fields."""
        op = self.spec['paths']['/api/status']['get']
        self.assertIn('tags', op)
        self.assertIn('operationId', op)
        # Overlay set the summary; auto fields still there.
        self.assertEqual(op['summary'],
                         "Get the worker's current state plus a few "
                         "aggregate counters.")

    def test_deep_merge_replaces_lists_does_not_concat(self):
        """The merge helper replaces lists wholesale rather than
        concatenating — OpenAPI list semantics are curated sets."""
        merged = self._deep_merge(
            {'a': [1, 2, 3], 'b': {'c': [4]}},
            {'a': [9],        'b': {'c': [5, 6]}},
        )
        self.assertEqual(merged['a'], [9])
        self.assertEqual(merged['b']['c'], [5, 6])

    def test_deep_merge_recurses_into_dicts(self):
        """Nested dict keys merge — `c` from base survives when
        overlay only overrides `d`."""
        merged = self._deep_merge(
            {'a': {'c': 1, 'd': 2}},
            {'a': {'d': 99}},
        )
        self.assertEqual(merged['a'], {'c': 1, 'd': 99})


if __name__ == '__main__':
    unittest.main()
