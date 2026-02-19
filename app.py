import os
import re
import io
import json
import time
import zipfile
import threading
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify, send_file
from curl_cffi import requests as cf_requests

app = Flask(__name__)
jobs = {}

IMPERSONATE = "chrome120"


def make_session(subdomain):
    session = cf_requests.Session(impersonate=IMPERSONATE)
    try:
        session.get("https://" + subdomain, timeout=15)
        time.sleep(0.8)
    except Exception:
        pass
    return session


def extract_album_info(url):
    parsed = urlparse(url)
    subdomain = parsed.hostname
    match = re.search(r'/albums/(\d+)', parsed.path)
    if not match:
        raise ValueError("Could not find album ID. Use a direct album link like: https://store.x.yupoo.com/albums/123456")
    album_id = match.group(1)
    return subdomain, album_id


def get_photo_id_from_url(url):
    """
    Yupoo CDN URLs look like:
      https://photo.yupoo.com/USERNAME/PHOTO_ID/FILE_HASH.jpg
    The PHOTO_ID (3rd path segment) is the same for all size variants of one photo.
    We group by USERNAME/PHOTO_ID to deduplicate.
    """
    parts = url.replace('https://', '').replace('http://', '').split('/')
    # parts: ['photo.yupoo.com', 'username', 'photo_id', 'file_hash.jpg']
    if len(parts) >= 3:
        return parts[1] + '/' + parts[2]  # username/photo_id
    return url  # fallback: use full url as key


def pick_largest_per_photo(session, subdomain, candidate_urls, job_id):
    """
    Group URLs by their photo_id. For each group, do a HEAD request
    to find the file with the largest Content-Length, which is the
    highest resolution. Returns one URL per unique photo.
    """
    # Group by photo_id
    groups = {}
    order = []
    for url in candidate_urls:
        pid = get_photo_id_from_url(url)
        if pid not in groups:
            groups[pid] = []
            order.append(pid)
        groups[pid].append(url)

    jobs[job_id]['message'] = 'Found ' + str(len(order)) + ' unique photos. Selecting highest resolution...'

    img_headers = {
        "Referer": "https://" + subdomain + "/",
        "Accept": "image/*,*/*;q=0.8",
    }

    result = []
    for i, pid in enumerate(order):
        urls_in_group = groups[pid]

        if len(urls_in_group) == 1:
            result.append(urls_in_group[0])
            continue

        # Multiple variants — pick the one with the largest file size
        best_url = urls_in_group[0]
        best_size = -1
        for url in urls_in_group:
            try:
                r = session.head(url, headers=img_headers, timeout=8)
                size = int(r.headers.get('content-length', 0))
                if size > best_size:
                    best_size = size
                    best_url = url
            except Exception:
                pass
            time.sleep(0.05)

        result.append(best_url)
        jobs[job_id]['message'] = 'Selecting best resolution: photo ' + str(i + 1) + ' of ' + str(len(order)) + '...'

    return result


def find_image_urls_in_json(obj, found=None):
    if found is None:
        found = []
    if isinstance(obj, str):
        if re.search(r'(?:photo|img)\.yupoo\.com', obj):
            url = obj if obj.startswith('http') else 'https:' + obj
            clean = url.split('?')[0]
            if clean not in found:
                found.append(clean)
    elif isinstance(obj, dict):
        for v in obj.values():
            find_image_urls_in_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_image_urls_in_json(item, found)
    return found


def collect_all_candidate_urls(session, subdomain, album_id, job_id):
    """Scrape the album and collect every image URL we can find (all sizes)."""
    base = "https://" + subdomain
    all_urls = []

    # Try known API endpoints first
    endpoints = [
        "/ajax/albums/{album_id}/photos?uid=1&page={page}&pageSize=30",
        "/api/albums/{album_id}/photos?uid=1&page={page}&pageSize=30",
    ]
    for endpoint_tpl in endpoints:
        page = 1
        found_any = False
        while True:
            url = base + endpoint_tpl.format(album_id=album_id, page=page)
            try:
                resp = session.get(url, headers={
                    "Referer": base + "/albums/" + album_id + "?uid=1",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, */*",
                }, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    found = find_image_urls_in_json(data)
                    if found:
                        new = [u for u in found if u not in all_urls]
                        all_urls.extend(new)
                        found_any = True
                        jobs[job_id]['message'] = 'API: collected ' + str(len(all_urls)) + ' candidates (page ' + str(page) + ')...'
                        total = None
                        if isinstance(data, dict):
                            total = data.get('total') or data.get('count')
                            if not total and isinstance(data.get('data'), dict):
                                total = data['data'].get('total') or data['data'].get('count')
                        if len(new) > 0 and (not total or len(all_urls) < int(total)):
                            page += 1
                            time.sleep(0.3)
                            continue
            except Exception:
                pass
            break
        if found_any:
            return all_urls

    # Fallback: scrape HTML pages
    page = 1
    while True:
        url = base + "/albums/" + album_id + "?uid=1&page=" + str(page)
        try:
            resp = session.get(url, headers={
                "Referer": base,
                "Accept": "text/html,*/*;q=0.8",
            }, timeout=20)
        except Exception as e:
            raise Exception("Failed to load album: " + str(e))

        html = resp.text
        found_on_page = []

        # Full https CDN URLs
        for m in re.findall(r'https?://(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+', html):
            m = m.rstrip('\\/"\' ')
            if re.search(r'\.(jpg|jpeg|png|webp)', m, re.I) and m not in all_urls and m not in found_on_page:
                found_on_page.append(m)

        # Protocol-relative
        for m in re.findall(r'//(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+', html):
            full = 'https:' + m.rstrip('\\/"\' ')
            if re.search(r'\.(jpg|jpeg|png|webp)', full, re.I) and full not in all_urls and full not in found_on_page:
                found_on_page.append(full)

        # Embedded JSON blobs
        for match in re.finditer(r'(?:window\.__\w+__|var \w+)\s*=\s*(\{[\s\S]{20,}?\})\s*;', html):
            try:
                data = json.loads(match.group(1))
                for u in find_image_urls_in_json(data):
                    if u not in all_urls and u not in found_on_page:
                        found_on_page.append(u)
            except Exception:
                pass

        all_urls.extend(found_on_page)
        jobs[job_id]['message'] = 'HTML scrape: ' + str(len(all_urls)) + ' candidates (page ' + str(page) + ')...'

        if not found_on_page or not re.search(r'page=' + str(page + 1), html):
            break
        page += 1
        time.sleep(0.5)

    return all_urls


def get_all_image_urls(subdomain, album_id, job_id):
    jobs[job_id]['status'] = 'fetching'
    jobs[job_id]['message'] = 'Starting session...'
    session = make_session(subdomain)

    # Step 1: collect all candidate URLs (all sizes)
    candidates = collect_all_candidate_urls(session, subdomain, album_id, job_id)

    if not candidates:
        return [], session

    # Step 2: group by photo ID and pick the largest file per photo
    best = pick_largest_per_photo(session, subdomain, candidates, job_id)
    return best, session


def download_and_zip(job_id, subdomain, album_id, image_urls, session, zip_name=None):
    zip_buffer = io.BytesIO()
    total = len(image_urls)
    downloaded = 0
    failed = 0

    jobs[job_id]['status'] = 'downloading'
    jobs[job_id]['total'] = total

    img_headers = {
        "Referer": "https://" + subdomain + "/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, url in enumerate(image_urls):
            jobs[job_id]['message'] = 'Downloading image ' + str(i + 1) + ' of ' + str(total) + '...'
            jobs[job_id]['downloaded'] = downloaded
            try:
                resp = session.get(url, headers=img_headers, timeout=30)
                resp.raise_for_status()

                content_type = resp.headers.get('content-type', '')
                ext = '.jpg'
                if 'png' in content_type:
                    ext = '.png'
                elif 'webp' in content_type:
                    ext = '.webp'
                elif 'gif' in content_type:
                    ext = '.gif'
                else:
                    m = re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', url, re.I)
                    if m:
                        ext = '.' + m.group(1).lower()

                filename = "image_" + str(i + 1).zfill(4) + ext
                zf.writestr(filename, resp.content)
                downloaded += 1
            except Exception as e:
                failed += 1
                print("Failed: " + url + " -> " + str(e))
            time.sleep(0.1)

    zip_buffer.seek(0)
    jobs[job_id]['status'] = 'done'
    jobs[job_id]['downloaded'] = downloaded
    jobs[job_id]['failed'] = failed
    jobs[job_id]['message'] = 'Done! Downloaded ' + str(downloaded) + ' images, ' + str(failed) + ' failed.'
    jobs[job_id]['zip_data'] = zip_buffer.getvalue()
    jobs[job_id]['zip_name'] = zip_name or ('yupoo_album_' + album_id + '.zip')


def run_job(job_id, url, zip_name=None):
    try:
        subdomain, album_id = extract_album_info(url)
        jobs[job_id]['album_id'] = album_id
        jobs[job_id]['subdomain'] = subdomain

        image_urls, session = get_all_image_urls(subdomain, album_id, job_id)
        jobs[job_id]['raw_urls'] = image_urls

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found. The album may be private or Yupoo changed their structure.'
            return

        jobs[job_id]['message'] = 'Found ' + str(len(image_urls)) + ' unique photos. Starting download...'

        custom_name = (zip_name or '').strip()
        if custom_name:
            safe = re.sub(r'[^\w\s\-]', '', custom_name).strip().replace(' ', '_')
            final_zip_name = safe + '.zip'
        else:
            final_zip_name = 'yupoo_album_' + album_id + '.zip'

        download_and_zip(job_id, subdomain, album_id, image_urls, session, final_zip_name)

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['message'] = 'Error: ' + str(e)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start', methods=['POST'])
def start_download():
    data = request.json
    url = data.get('url', '').strip()
    zip_name = data.get('zip_name', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if 'yupoo.com/albums/' not in url:
        return jsonify({'error': 'Please provide a valid Yupoo album URL (must contain /albums/)'}), 400

    import uuid
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'starting',
        'message': 'Starting...',
        'downloaded': 0,
        'total': 0,
        'failed': 0,
        'zip_data': None,
        'raw_urls': [],
    }

    thread = threading.Thread(target=run_job, args=(job_id, url, zip_name))
    thread.daemon = True
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'status': job['status'],
        'message': job['message'],
        'downloaded': job['downloaded'],
        'total': job['total'],
        'failed': job.get('failed', 0),
        'ready': job['status'] == 'done',
        'raw_urls': job.get('raw_urls', []),
    })


@app.route('/download/<job_id>')
def download_zip(job_id):
    job = jobs.get(job_id)
    if not job or not job.get('zip_data'):
        return jsonify({'error': 'ZIP not ready'}), 404
    return send_file(
        io.BytesIO(job['zip_data']),
        mimetype='application/zip',
        as_attachment=True,
        download_name=job.get('zip_name', 'yupoo_album.zip')
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
