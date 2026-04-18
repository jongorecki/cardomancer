# web_database.py
# ---------------------------------------------------------------------------
# Scryfall database update pipeline for the web server.
# Wraps existing download/hash scripts into a single callable pipeline
# with progress reporting via a callback function.
# ---------------------------------------------------------------------------

import os
import json
import glob
import threading
import time

from config import SCRIPT_DIR, CARDS_JSON_PATH, HASH_DB_PATH, HASH_DB_V2_PATH


class DatabaseUpdater:
    """
    Manages the Scryfall database update pipeline:
    1. Check for newer bulk data
    2. Download new JSON
    3. Build printings map
    4. Download missing card images
    5. Generate v1 hash DB
    6. Generate v2 hash DB
    7. Update config
    """

    def __init__(self):
        self.running = False
        self.step = ''
        self.progress = 0
        self.total = 0
        self.message = ''
        self._thread = None
        self._emit_fn = None

    def set_emit(self, emit_fn):
        self._emit_fn = emit_fn

    def emit_progress(self, step, progress, total, message):
        self.step = step
        self.progress = progress
        self.total = total
        self.message = message
        if self._emit_fn:
            self._emit_fn('db_update_progress', {
                'step': step,
                'progress': progress,
                'total': total,
                'message': message,
                'running': self.running,
            })

    def get_current_db_info(self):
        """Get info about the currently loaded database."""
        info = {
            'cards_json': os.path.basename(CARDS_JSON_PATH),
            'cards_json_exists': os.path.exists(CARDS_JSON_PATH),
            'hash_db_v1_exists': os.path.exists(HASH_DB_PATH),
            'hash_db_v2_exists': os.path.exists(HASH_DB_V2_PATH),
        }

        # Card count from JSON
        if os.path.exists(CARDS_JSON_PATH):
            info['cards_json_size_mb'] = round(
                os.path.getsize(CARDS_JSON_PATH) / (1024 * 1024), 1)
            # Try to get count from the loaded cards module
            try:
                from cards import CARD_DATA_BY_ID
                info['card_count'] = len(CARD_DATA_BY_ID)
            except Exception:
                info['card_count'] = '?'
        else:
            info['cards_json_size_mb'] = 0
            info['card_count'] = 0

        # Hash DB sizes
        for path_key, path in [('hash_db_v1', HASH_DB_PATH),
                               ('hash_db_v2', HASH_DB_V2_PATH)]:
            if os.path.exists(path):
                info[f'{path_key}_size_mb'] = round(
                    os.path.getsize(path) / (1024 * 1024), 1)
                try:
                    with open(path, 'r') as f:
                        data = json.load(f)
                    info[f'{path_key}_count'] = len(data)
                except Exception:
                    info[f'{path_key}_count'] = '?'

        # Image directory
        images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')
        if os.path.isdir(images_dir):
            png_files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]
            info['image_count'] = len(png_files)
            # Approximate disk usage
            total_size = sum(
                os.path.getsize(os.path.join(images_dir, f))
                for f in png_files[:100]
            )
            if png_files:
                avg_size = total_size / min(len(png_files), 100)
                info['images_size_gb'] = round(
                    avg_size * len(png_files) / (1024 * 1024 * 1024), 2)
        else:
            info['image_count'] = 0
            info['images_size_gb'] = 0

        return info

    def check_for_update(self):
        """Check Scryfall API for newer bulk data. Returns update info."""
        import requests
        try:
            resp = requests.get("https://api.scryfall.com/bulk-data", timeout=10)
            resp.raise_for_status()
            catalog = resp.json()

            for entry in catalog.get('data', []):
                if entry.get('type') == 'default_cards':
                    remote_uri = entry['download_uri']
                    remote_filename = remote_uri.split('/')[-1].split('?')[0]
                    current_filename = os.path.basename(CARDS_JSON_PATH)
                    needs_update = remote_filename != current_filename
                    return {
                        'available': True,
                        'needs_update': needs_update,
                        'current_file': current_filename,
                        'remote_file': remote_filename,
                        'updated_at': entry.get('updated_at', '?'),
                        'size': entry.get('size', 0),
                    }

            return {'available': False, 'error': 'default_cards not found in catalog'}
        except Exception as e:
            return {'available': False, 'error': str(e)}

    def start_update(self):
        """Start the full update pipeline in a background thread."""
        if self.running:
            return False
        self.running = True
        self._thread = threading.Thread(target=self._run_update, daemon=True)
        self._thread.start()
        return True

    def start_prices_refresh(self):
        """
        Start a FAST prices-only refresh in a background thread.

        This downloads the latest Scryfall bulk data, rebuilds the
        printings map, points config.py at the new JSON, and reloads
        the in-memory card data. It DOES NOT re-download card images
        or regenerate hash databases — those are image-derived and
        have nothing to do with prices. Typically completes in under
        a minute (vs 30+ minutes for a full update).

        Use this when prices have gone stale but the hash DB is
        still fine. Use start_update() instead when new sets have
        been released and you need new card images + hashes.
        """
        if self.running:
            return False
        self.running = True
        self._thread = threading.Thread(
            target=self._run_prices_refresh, daemon=True)
        self._thread.start()
        return True

    def _run_update(self):
        """Run the full update pipeline."""
        try:
            self._step_download_bulk_data()
            if not self.running:
                return
            self._step_build_printings_map()
            if not self.running:
                return
            self._step_download_images()
            if not self.running:
                return
            self._step_generate_v1_hashes()
            if not self.running:
                return
            self._step_generate_v2_hashes()
            if not self.running:
                return
            self._step_update_config()
            self._step_reload_card_data()

            self.emit_progress('complete', 1, 1, 'Database update complete!')
        except Exception as e:
            self.emit_progress('error', 0, 0, f'Update failed: {e}')
        finally:
            self.running = False

    def _run_prices_refresh(self):
        """
        Run the prices-only refresh pipeline: bulk data + printings
        map + config update + in-place reload. Skips image download
        and hash DB regeneration entirely.
        """
        try:
            self._step_download_bulk_data()
            if not self.running:
                return
            self._step_build_printings_map()
            if not self.running:
                return
            self._step_update_config()
            if not self.running:
                return
            self._step_reload_card_data()

            self.emit_progress('complete', 1, 1,
                               'Prices refresh complete!')
        except Exception as e:
            self.emit_progress('error', 0, 0,
                               f'Prices refresh failed: {e}')
        finally:
            self.running = False

    def _step_reload_card_data(self):
        """
        Final step: reload the in-memory card data from the new
        JSON file so the running web server picks up fresh prices
        without a restart. Mutates CARDS_DATA / CARD_DATA_BY_ID /
        PRINTINGS_MAP in place (see cards.reload_card_data()).
        """
        self.emit_progress('reload_cards', 0, 1,
                           'Reloading card data into memory...')
        try:
            from cards import reload_card_data
            count = reload_card_data()
            self.emit_progress('reload_cards', 1, 1,
                               f'Reloaded {count} cards into memory '
                               f'(prices now current)')
        except Exception as e:
            self.emit_progress('reload_cards', 1, 1,
                               f'In-memory reload failed: {e}. '
                               f'Restart the web server to pick up '
                               f'the new data.')

    def _step_download_bulk_data(self):
        """Step 1: Download latest Scryfall bulk data."""
        self.emit_progress('download_bulk', 0, 1, 'Downloading Scryfall bulk data...')

        from download_cards import download_scryfall_bulk_data
        json_path = download_scryfall_bulk_data()

        if json_path is None:
            existing = sorted(glob.glob(os.path.join(SCRIPT_DIR, "default-cards-*.json")))
            if existing:
                json_path = existing[-1]
            else:
                raise RuntimeError("No Scryfall bulk data available")

        self._json_path = json_path
        self.emit_progress('download_bulk', 1, 1,
                          f'Bulk data ready: {os.path.basename(json_path)}')

    def _step_build_printings_map(self):
        """Step 2: Load cards and build printings map."""
        self.emit_progress('printings_map', 0, 1, 'Loading card data...')

        with open(self._json_path, 'r', encoding='utf-8') as f:
            all_cards = json.load(f)

        self.emit_progress('printings_map', 0, 1,
                          f'Building printings map from {len(all_cards)} cards...')

        from download_cards import build_printings_map
        self._representative_cards, self._printings_map = build_printings_map(all_cards)

        # Save printings map
        map_path = os.path.join(SCRIPT_DIR, "printings_map.json")
        with open(map_path, 'w', encoding='utf-8') as f:
            json.dump(self._printings_map, f, ensure_ascii=False, indent=2)

        self.emit_progress('printings_map', 1, 1,
                          f'Printings map: {len(self._printings_map)} entries')

    def _step_download_images(self):
        """Step 3: Download missing card images."""
        images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')
        os.makedirs(images_dir, exist_ok=True)

        # Count how many need downloading
        to_download = []
        for card in self._representative_cards:
            card_id = card.get('id', '')
            if not card_id or 'image_uris' not in card:
                continue
            output_path = os.path.join(images_dir, f"{card_id}.png")
            if not os.path.exists(output_path):
                to_download.append(card)

        total = len(to_download)
        self.emit_progress('download_images', 0, total,
                          f'Downloading {total} new card images...')

        if total == 0:
            self.emit_progress('download_images', 0, 0, 'All images already downloaded')
            return

        import requests
        downloaded = 0
        errors = 0
        for card in to_download:
            if not self.running:
                return
            card_id = card['id']
            image_uri = card['image_uris'].get('png')
            if not image_uri:
                continue

            output_path = os.path.join(images_dir, f"{card_id}.png")
            try:
                resp = requests.get(image_uri, stream=True, timeout=30)
                resp.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded += 1
            except Exception:
                errors += 1

            if downloaded % 100 == 0 or downloaded == total:
                self.emit_progress('download_images', downloaded, total,
                                  f'Downloaded {downloaded}/{total} images '
                                  f'({errors} errors)')
            time.sleep(0.1)  # Rate limit

        self.emit_progress('download_images', total, total,
                          f'Images done: {downloaded} downloaded, {errors} errors')

    def _step_generate_v1_hashes(self):
        """Step 4: Generate v1 hash database."""
        images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')
        if not os.path.isdir(images_dir):
            self.emit_progress('hash_v1', 0, 0, 'No images directory found')
            return

        files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]
        total = len(files)
        self.emit_progress('hash_v1', 0, total,
                          f'Generating v1 hashes for {total} images...')

        from importlib import import_module
        # Use the existing hash creation function
        hash_mod = import_module('16bit_rgb_create_card_hashes')
        hash_mod.create_color_hash_database(
            images_dir, HASH_DB_PATH, crop_size=745, hash_size=16
        )

        self.emit_progress('hash_v1', total, total, 'v1 hash database complete')

    def _step_generate_v2_hashes(self):
        """Step 5: Generate v2 hash database."""
        images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')
        if not os.path.isdir(images_dir):
            self.emit_progress('hash_v2', 0, 0, 'No images directory found')
            return

        files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]
        total = len(files)
        self.emit_progress('hash_v2', 0, total,
                          f'Generating v2 hashes for {total} images...')

        # Use the existing v2 hash creation main()
        from create_card_hashes_v2 import process_image, ART_REGION, HASH_SIZE
        from concurrent.futures import ProcessPoolExecutor

        args_list = [(f, images_dir, HASH_SIZE) for f in files]
        hash_db = {}
        done = 0

        with ProcessPoolExecutor() as executor:
            for result in executor.map(process_image, args_list):
                card_id, card_hashes, error_msg = result
                if card_id and card_hashes:
                    hash_db[card_id] = card_hashes
                done += 1
                if done % 5000 == 0:
                    self.emit_progress('hash_v2', done, total,
                                      f'v2 hashing: {done}/{total}')

        with open(HASH_DB_V2_PATH, 'w', encoding='utf-8') as f:
            json.dump(hash_db, f, ensure_ascii=False)

        self.emit_progress('hash_v2', total, total,
                          f'v2 hash database complete ({len(hash_db)} entries)')

    def _step_update_config(self):
        """Step 6: Update config.py to point to new bulk data file."""
        new_filename = os.path.basename(self._json_path)
        config_path = os.path.join(SCRIPT_DIR, 'config.py')

        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Replace the CARDS_JSON_PATH line
        import re
        old_pattern = r'CARDS_JSON_PATH\s*=\s*os\.path\.join\(SCRIPT_DIR,\s*"[^"]+"\)'
        new_line = f'CARDS_JSON_PATH = os.path.join(SCRIPT_DIR, "{new_filename}")'

        if re.search(old_pattern, content):
            new_content = re.sub(old_pattern, new_line, content)
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            self.emit_progress('update_config', 1, 1,
                              f'Config updated to use {new_filename}')
        else:
            self.emit_progress('update_config', 1, 1,
                              f'Config not updated (pattern not found). '
                              f'Manually set CARDS_JSON_PATH to "{new_filename}"')

    def stop(self):
        """Cancel a running update."""
        self.running = False

    def get_status(self):
        """Get current update status."""
        return {
            'running': self.running,
            'step': self.step,
            'progress': self.progress,
            'total': self.total,
            'message': self.message,
        }


# Module-level singleton
db_updater = DatabaseUpdater()
