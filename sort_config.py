# sort_config.py
# Loads and manages sort configurations for custom Scryfall-like query sorting.
# Supports loading from file, manual input, and pre-fetching otag data.

import os
import re
import time

from query_parser import (
    parse_query, evaluate_query, collect_otag_terms,
    collect_enrichment_fields, expand_otag_cache, QueryParseError,
)


class SortConfig:
    """
    Holds a custom sort configuration: bin count, fallback bin, and ordered
    list of (bin_number, query_string, parsed_ast) for bin assignment.

    Override bins (optional):
        Bins listed in ``override_bins`` are evaluated BEFORE the regular
        bin_queries. Use-case: "if the card is over $1, put it in bin 8,
        otherwise do my usual color sort." Declared in the config file as
        ``overrides: 3,5`` — the listed bin numbers are pulled out of the
        normal flow and checked first, in the order given.
    """

    def __init__(self, bin_count, fallback_bin, bin_queries, bin_limit=None,
                 otag_cache=None, override_bins=None):
        """
        bin_count:    total number of bins
        fallback_bin: bin number for cards that don't match any query
        bin_queries:  list of (bin_number, query_string, ast) in evaluation order
        bin_limit:    global max cards per bin (int), or None for unlimited.
                      When a bin reaches this limit, cards overflow to the next
                      bin with a matching query.
        otag_cache:   dict of { tag_name: set(oracle_ids) } for otag: queries
        override_bins: list of bin numbers that take priority over the regular
                       bin order. Checked first, in the order given. A bin
                       listed here is NOT also checked in the regular pass.
        """
        self.bin_count = bin_count
        self.fallback_bin = fallback_bin
        self.bin_queries = bin_queries
        self.bin_limit = bin_limit
        self.otag_cache = otag_cache or {}
        self.override_bins = list(override_bins or [])
        # Precompute the split so get_bin() stays a flat walk — overrides
        # first (in override_bins order), then regular bins (in declaration
        # order). Entries with the same bin number stay grouped together
        # to preserve overflow semantics.
        override_set = set(self.override_bins)
        by_bin: dict = {}
        for entry in bin_queries:
            by_bin.setdefault(entry[0], []).append(entry)
        self._override_queries = []
        for ov_bn in self.override_bins:
            for entry in by_bin.get(ov_bn, []):
                self._override_queries.append(entry)
        self._regular_queries = [
            entry for entry in bin_queries if entry[0] not in override_set
        ]
        # Track how many cards have been placed in each bin
        self.bin_card_counts = {}
        # Enrichment support: populated by from_lines/from_file when needed
        self._needs_enrichment = False   # True if any query uses staple/salt/combo
        self._enr_cache: dict = {}       # oracle_id → enrichment data dict

    def get_bin(self, card_data):
        """
        Evaluate the card against bin queries in order.

        Evaluation passes, in order:
          1. Override bins (listed in ``overrides:`` directive) — checked
             first, in the order given. First match wins.
          2. Regular bin queries — declaration order. First match wins.
          3. Fallback bin — if nothing matched.

        When a bin has reached the global bin_limit, the card falls through
        to the next bin with a matching query — this is how overflow works.
        Multiple bins with the same query form a natural overflow chain.
        Overflow stays within its pass (an overflowed override bin falls
        through to the next override bin, not back to regular bins).
        """
        if not card_data:
            return self.fallback_bin

        # Fetch enrichment data once per card when any query needs it
        enrichment_data = None
        if self._needs_enrichment:
            oracle_id = card_data.get('oracle_id')
            if oracle_id:
                enrichment_data = self._get_enrichment_data(oracle_id)

        # Two-pass evaluation: overrides first, then regular.
        for queries in (self._override_queries, self._regular_queries):
            for bin_num, query_str, ast in queries:
                # Skip bins that are at capacity
                if self.bin_limit is not None:
                    current = self.bin_card_counts.get(bin_num, 0)
                    if current >= self.bin_limit:
                        continue

                try:
                    if evaluate_query(ast, card_data, self.otag_cache,
                                      enrichment_data=enrichment_data):
                        self.bin_card_counts[bin_num] = \
                            self.bin_card_counts.get(bin_num, 0) + 1
                        return bin_num
                except Exception as e:
                    print(f"[sort_config] Error evaluating bin {bin_num} "
                          f"query '{query_str}': {e}")
                    continue

        # Nothing matched — log once (first 5 fallbacks) so the user can
        # see which queries are failing without flooding the console.
        fb_count = self.bin_card_counts.get(self.fallback_bin, 0)
        if fb_count < 5:
            card_name = card_data.get('name', '?') if card_data else '?'
            tried = ', '.join(
                f'bin{b}({q!r})' for b, q, _ in self.bin_queries
            ) or '(none)'
            print(
                f"[sort_config] FALLBACK: '{card_name}' matched no queries "
                f"[{tried}] → bin {self.fallback_bin}",
                flush=True,
            )
        self.bin_card_counts[self.fallback_bin] = fb_count + 1
        return self.fallback_bin

    def _get_enrichment_data(self, oracle_id: str) -> dict:
        """Fetch enrichment data for one oracle_id, with in-session cache.

        Returns a dict with keys:
          staple_universal, staple_cedh, staple_archetype (bool)
          salt (float|None)
          in_combo (bool)
        Degrades to {} if enrichment.db is unavailable.
        """
        if oracle_id in self._enr_cache:
            return self._enr_cache[oracle_id]

        data: dict = {}
        try:
            import enrichment_db
            conn = enrichment_db.get_connection()
            try:
                tiers = {
                    r[0] for r in conn.execute(
                        "SELECT tier FROM staples WHERE oracle_id=?",
                        (oracle_id,),
                    ).fetchall()
                }
                salt_row = conn.execute(
                    "SELECT salt FROM salt_scores WHERE oracle_id=?",
                    (oracle_id,),
                ).fetchone()
                combo_row = conn.execute(
                    "SELECT 1 FROM combo_membership WHERE oracle_id=? LIMIT 1",
                    (oracle_id,),
                ).fetchone()
                data = {
                    "staple_universal":  "universal"  in tiers,
                    "staple_cedh":       "cedh"       in tiers,
                    "staple_archetype":  "archetype"  in tiers,
                    "salt":              salt_row[0] if salt_row else None,
                    "in_combo":          combo_row is not None,
                }
            finally:
                conn.close()
        except Exception as exc:
            print(f"[sort_config] enrichment lookup failed for {oracle_id}: {exc}")

        self._enr_cache[oracle_id] = data
        return data

    def reset_counts(self):
        """Reset all bin card counts (e.g., when starting a new sorting session)."""
        self.bin_card_counts = {}

    def get_status(self):
        """Return a string showing current card counts per bin."""
        lines = []
        limit_str = f"/{self.bin_limit}" if self.bin_limit is not None else ""
        override_set = set(self.override_bins)
        for bin_num, query_str, _ast in self.bin_queries:
            count = self.bin_card_counts.get(bin_num, 0)
            marker = "★ " if bin_num in override_set else ""
            lines.append(f"  {marker}Bin {bin_num}: {count}{limit_str} cards — {query_str}")
        fb_count = self.bin_card_counts.get(self.fallback_bin, 0)
        lines.append(f"  Bin {self.fallback_bin} (fallback): {fb_count}{limit_str} cards")
        return '\n'.join(lines)

    def describe(self):
        """Return a human-readable summary of the sort config."""
        limit_info = f", limit={self.bin_limit}/bin" if self.bin_limit else ""
        override_info = (
            f", overrides={self.override_bins}" if self.override_bins else ""
        )
        lines = [
            f"Custom Sort Config: {self.bin_count} bins, "
            f"fallback=bin {self.fallback_bin}{limit_info}{override_info}"
        ]
        override_set = set(self.override_bins)
        for bin_num, query_str, _ast in self.bin_queries:
            marker = "★ " if bin_num in override_set else ""
            lines.append(f"  {marker}Bin {bin_num}: {query_str}")
        unused = set(range(1, self.bin_count + 1))
        used = {bq[0] for bq in self.bin_queries}
        unused -= used
        unused.discard(self.fallback_bin)
        if unused:
            lines.append(f"  Unused bins: {sorted(unused)}")
        if self.otag_cache:
            for tag, ids in self.otag_cache.items():
                lines.append(f"  [otag:{tag} — {len(ids)} cards cached]")
        return '\n'.join(lines)

    @classmethod
    def from_lines(cls, lines):
        """
        Parse a sort config from a list of strings.
        Format:
            bins: 10
            fallback: 10
            overrides: 3,5          (optional — comma-separated bin numbers)
            limit: 50               (optional)
            bin1: c:w t:creature
            bin2: usd>=10
            ...

        The ``overrides:`` directive lists bins that are evaluated BEFORE the
        regular bin order. Use it to "pull out" high-priority bins (e.g.
        "cards over $1 always go here, regardless of color").
        """
        bin_count = None
        fallback_bin = None
        bin_limit = None
        override_bins: list = []
        bin_queries = []

        for line_num, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue

            # Parse directives
            if line.lower().startswith('bins:'):
                try:
                    bin_count = int(line.split(':', 1)[1].strip())
                except ValueError:
                    raise ValueError(
                        f"Line {line_num}: Invalid bin count: {line}"
                    )
                continue

            if line.lower().startswith('fallback:'):
                try:
                    fallback_bin = int(line.split(':', 1)[1].strip())
                except ValueError:
                    raise ValueError(
                        f"Line {line_num}: Invalid fallback bin: {line}"
                    )
                continue

            if line.lower().startswith('limit:'):
                try:
                    bin_limit = int(line.split(':', 1)[1].strip())
                    if bin_limit < 1:
                        raise ValueError("must be positive")
                except ValueError:
                    raise ValueError(
                        f"Line {line_num}: Invalid limit: {line}"
                    )
                continue

            if line.lower().startswith('overrides:'):
                raw = line.split(':', 1)[1].strip()
                if not raw:
                    override_bins = []
                    continue
                try:
                    parts = [p.strip() for p in raw.split(',') if p.strip()]
                    override_bins = [int(p) for p in parts]
                except ValueError:
                    raise ValueError(
                        f"Line {line_num}: Invalid overrides list "
                        f"(expected comma-separated ints): {line}"
                    )
                continue

            # Parse bin definitions: "bin3: query here"
            bin_match = re.match(r'^bin(\d+)\s*:\s*(.+)$', line, re.IGNORECASE)
            if bin_match:
                bin_num = int(bin_match.group(1))
                query_str = bin_match.group(2).strip()

                try:
                    ast = parse_query(query_str)
                except QueryParseError as e:
                    raise ValueError(
                        f"Line {line_num}: Invalid query for bin {bin_num}: {e}"
                    )

                bin_queries.append((bin_num, query_str, ast))
                continue

            raise ValueError(
                f"Line {line_num}: Unrecognized line: {line}"
            )

        # Validate
        if bin_count is None:
            raise ValueError("Missing 'bins:' directive")
        if fallback_bin is None:
            raise ValueError("Missing 'fallback:' directive")
        if fallback_bin < 1 or fallback_bin > bin_count:
            raise ValueError(
                f"Fallback bin {fallback_bin} is out of range (1-{bin_count})"
            )
        for bin_num, query_str, _ast in bin_queries:
            if bin_num < 1 or bin_num > bin_count:
                raise ValueError(
                    f"Bin {bin_num} is out of range (1-{bin_count})"
                )
        # Validate override_bins — must be in-range, must have a query
        # (evaluating an empty bin as override is a silent no-op, confusing).
        defined_bins = {bq[0] for bq in bin_queries}
        for ov_bn in override_bins:
            if ov_bn < 1 or ov_bn > bin_count:
                raise ValueError(
                    f"Override bin {ov_bn} is out of range (1-{bin_count})"
                )
            if ov_bn == fallback_bin:
                raise ValueError(
                    f"Override bin {ov_bn} cannot be the fallback bin"
                )
            if ov_bn not in defined_bins:
                raise ValueError(
                    f"Override bin {ov_bn} has no query defined"
                )

        config = cls(bin_count, fallback_bin, bin_queries,
                     bin_limit=bin_limit, override_bins=override_bins)

        # Pre-fetch otag data if any queries use otag:
        all_otags = set()
        for _bn, _qs, ast in bin_queries:
            all_otags.update(collect_otag_terms(ast))
        if all_otags:
            config.otag_cache = fetch_otag_data(all_otags)

        # Flag if any queries use enrichment tokens (staple/salt/combo)
        for _bn, _qs, ast in bin_queries:
            if collect_enrichment_fields(ast):
                config._needs_enrichment = True
                break
        if config._needs_enrichment:
            print("[sort_config] Enrichment tokens detected — "
                  "staple/salt/combo will be resolved per card from enrichment.db")

        return config

    @classmethod
    def from_file(cls, filepath):
        """Load a sort config from a text file."""
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Sort config file not found: {filepath}")

        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        print(f"[sort_config] Loading config from: {filepath}")
        config = cls.from_lines(lines)
        print(f"[sort_config] {config.describe()}")
        return config

    def to_lines(self, description: str = "") -> list:
        """
        Serialize this config back to the textual file format.
        Round-trip with from_lines() is the contract the web UI relies on
        when saving edited configs as new .txt presets.
        """
        out: list = []
        if description:
            for desc_line in description.splitlines():
                out.append(f"# {desc_line}")
            out.append("")
        out.append(f"bins: {self.bin_count}")
        out.append(f"fallback: {self.fallback_bin}")
        if self.bin_limit is not None:
            out.append(f"limit: {self.bin_limit}")
        if self.override_bins:
            out.append(f"overrides: {','.join(str(b) for b in self.override_bins)}")
        out.append("")
        for bin_num, query_str, _ast in self.bin_queries:
            out.append(f"bin{bin_num}: {query_str}")
        return out


def prompt_manual_config():
    """
    Interactive prompt to build a sort config from the command line.
    Returns a SortConfig.
    """
    print("\n--- Custom Sort Configuration ---")
    print("Define bin queries using Scryfall-like syntax.")
    print("Examples: c:w  t:creature  usd>=10  cmc<=3  r:mythic")
    print("          c:w or c:u  (c:r or c:g) t:creature  -t:land")
    print("          otag:removal  kw:flying  legal:commander")
    print()

    while True:
        try:
            bin_count = int(input("How many bins? ").strip())
            if bin_count < 1:
                print("Must have at least 1 bin.")
                continue
            break
        except ValueError:
            print("Enter a number.")

    while True:
        try:
            fallback_bin = int(
                input(f"Fallback bin for unmatched cards (1-{bin_count})? ").strip()
            )
            if 1 <= fallback_bin <= bin_count:
                break
            print(f"Must be between 1 and {bin_count}.")
        except ValueError:
            print("Enter a number.")

    limit_str = input("Max cards per bin (blank for unlimited)? ").strip()
    bin_limit = None
    if limit_str:
        try:
            bin_limit = int(limit_str)
            if bin_limit < 1:
                print("Invalid, using unlimited.")
                bin_limit = None
        except ValueError:
            print("Invalid, using unlimited.")

    print(f"\nNow define queries for each bin. Leave blank to skip a bin.")
    print(f"Bin {fallback_bin} is the fallback (catches unmatched cards).")
    if bin_limit:
        print(f"Each bin holds up to {bin_limit} cards before overflowing.")
    print()

    bin_queries = []
    for i in range(1, bin_count + 1):
        if i == fallback_bin:
            print(f"  Bin {i}: [fallback — catches all unmatched cards]")
            continue

        while True:
            query_str = input(f"  Bin {i} query (or blank to skip): ").strip()
            if not query_str:
                break

            try:
                ast = parse_query(query_str)
                bin_queries.append((i, query_str, ast))
                break
            except QueryParseError as e:
                print(f"    Invalid query: {e}")
                print(f"    Try again.")

    config = SortConfig(bin_count, fallback_bin, bin_queries,
                        bin_limit=bin_limit)

    # Pre-fetch otag data
    all_otags = set()
    for _bn, _qs, ast in bin_queries:
        all_otags.update(collect_otag_terms(ast))
    if all_otags:
        config.otag_cache = fetch_otag_data(all_otags)

    # Flag enrichment token usage
    for _bn, _qs, ast in bin_queries:
        if collect_enrichment_fields(ast):
            config._needs_enrichment = True
            break
    if config._needs_enrichment:
        print("[sort_config] Enrichment tokens detected — "
              "staple/salt/combo will be resolved per card from enrichment.db")

    print(f"\n{config.describe()}")
    return config


# ---------------------------------------------------------------------------
# Scryfall API: otag pre-fetching
# ---------------------------------------------------------------------------

def fetch_otag_data(tag_names):
    """
    Fetch oracle_ids for each otag from the Scryfall API.
    Returns { tag_name: set(oracle_ids) }.

    Paginates through all results. Respects Scryfall rate limit (100ms between requests).
    """
    import requests

    cache = {}
    for tag in sorted(tag_names):
        print(f"[otag] Fetching otag:{tag} from Scryfall API...")
        oracle_ids = set()
        url = 'https://api.scryfall.com/cards/search'
        params = {
            'q': f'otag:{tag}',
            'unique': 'cards',
            'format': 'json',
        }

        page = 0
        while True:
            page += 1
            try:
                resp = requests.get(url, params=params)
                if resp.status_code == 404:
                    # No results for this tag
                    print(f"[otag] No cards found for otag:{tag}")
                    break
                resp.raise_for_status()
                data = resp.json()

                for card in data.get('data', []):
                    oid = card.get('oracle_id')
                    if oid:
                        oracle_ids.add(oid)

                if page % 10 == 0:
                    print(f"[otag]   ... page {page}, {len(oracle_ids)} cards so far")

                if not data.get('has_more'):
                    break

                # Next page URL
                url = data.get('next_page', '')
                params = {}  # next_page URL includes params
                time.sleep(0.1)  # respect rate limit

            except Exception as e:
                print(f"[otag] Error fetching otag:{tag} page {page}: {e}")
                break

        cache[tag] = oracle_ids
        print(f"[otag] otag:{tag} — {len(oracle_ids)} unique cards cached")

    # Expand parent tags to include descendant tags' oracle_ids using
    # enrichment_db tag_catalog hierarchy (degrades gracefully if unavailable).
    try:
        import enrichment_db
        conn = enrichment_db.get_connection()
        try:
            cache = expand_otag_cache(cache, conn)
        finally:
            conn.close()
    except Exception:
        pass

    return cache
