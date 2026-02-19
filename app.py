import os
import re
import io
import json
import time
import zipfile
import threading
from urllib.parse import urlparse, urlencode
from flask import Flask, render_template, request, jsonify, send_file
from curl_cffi import requests as cf_requests

app = Flask(__name__)
jobs = {}

# Impersonate Chrome 120 — full TLS fingerprint spoofing
IMPERSONATE = "chrome120"


def make_session(subdomain):
    session = cf_requests.Session(impersonate=IMPERSONATE)
    # Warm up with a visit to the store homepage to get cookies
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


def is_big_image(url):
    """Return True only if this URL is a 'big' quality Yupoo image."""
    # Yupoo big images have 'big' in the filename or path segment
    # e.g. photo.yupoo.com/user/albumid/big_filename.jpg
    #      photo.yupoo.com/user/albumid/filename_big.jpg
    #      photo.yupoo.com/.../.../big/filename.jpg
    filename = url.split('/')[-1].split('?')[0].lower()
    path = url.lower()
    return 'big' in filename or '/big/' in path or '_big.' in path


def upgrade_to_big(url):
    """Try to convert a non-big URL to its 'big' equivalent."""
    # Common Yupoo pattern: replace size prefix like 'small', 'medium', 'thumb' with 'big'
    for size in ['small', 'medium', 'thumb', 'normal', 'mini', 'sq']:
        if size in url.lower():
            upgraded = re.sub(re.escape(size), 'big', url, flags=re.IGNORECASE)
            return upgraded
    # Also try inserting 'big' before the filename if no size found
    return url


def filter_and_upgrade_urls(urls):
    """From a list of image URLs, keep only 'big' ones.
    If a URL is not 'big', try to upgrade it. Return deduplicated list."""
    result = []
    seen = set()
    for url in urls:
        if is_big_image(url):
            if url not in seen:
                result.append(url)
                seen.add(url)
        else:
            upgraded = upgrade_to_big(url)
            if upgraded not in seen:
                result.append(upgraded)
                seen.add(upgraded)
    return result


def find_image_urls_in_json(obj, found=None):
    if found is None:
        found = []
    if isinstance(obj, str):
        if re.search(r'(?:photo|img)\.yupoo\.com', obj):
            url = obj if obj.startswith('http') else 'https:' + obj
            clean = url.split('?')[0]  # remove query params
            if clean not in found:
                found.append(clean)
    elif isinstance(obj, dict):
        for v in obj.values():
            find_image_urls_in_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_image_urls_in_json(item, found)
    return found


def try_api_endpoints(session, subdomain, album_id, job_id):
    """Try Yupoo's known internal API endpoints."""
    base = "https://" + subdomain
    all_urls = []

    # Known Yupoo API patterns (discovered via network inspection)
    endpoints = [
        "/ajax/albums/{album_id}/photos?uid=1&page={page}&pageSize=30",
        "/api/albums/{album_id}/photos?uid=1&page={page}&pageSize=30",
        "/albums/{album_id}/photos?uid=1&page={page}&pageSize=30&format=json",
    ]

    for endpoint_tpl in endpoints:
        page = 1
        found_any = False
        while True:
            endpoint = endpoint_tpl.format(album_id=album_id, page=page)
            url = base + endpoint
            try:
                resp = session.get(
                    url,
                    headers={
                        "Referer": base + "/albums/" + album_id + "?uid=1",
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                    },
                    timeout=15
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        found = find_image_urls_in_json(data)
                        if found:
                            new = [u for u in found if u not in all_urls]
                            all_urls.extend(new)
                            found_any = True
                            jobs[job_id]['message'] = 'API found ' + str(len(all_urls)) + ' images (page ' + str(page) + ')...'
                            # Check if more pages
                            total = None
                            if isinstance(data, dict):
                                total = data.get('total') or data.get('count') or data.get('totalCount')
                                if not total and 'data' in data:
                                    d = data['data']
                                    if isinstance(d, dict):
                                        total = d.get('total') or d.get('count')
                            if total and len(all_urls) < int(total):
                                page += 1
                                time.sleep(0.3)
                                continue
                            else:
                                if len(new) > 0:
                                    page += 1
                                    time.sleep(0.3)
                                    continue
                    except Exception:
                        pass
            except Exception:
                pass
            break

        if found_any and all_urls:
            return filter_and_upgrade_urls(all_urls)

    return filter_and_upgrade_urls(all_urls)


def scrape_html_for_images(session, subdomain, album_id, job_id):
    """Load album HTML and aggressively extract all image references."""
    base = "https://" + subdomain
    all_urls = []
    page = 1

    while True:
        url = base + "/albums/" + album_id
        params = "uid=1&page=" + str(page)
        try:
            resp = session.get(
                url + "?" + params,
                headers={
                    "Referer": base,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=20
            )
        except Exception as e:
            raise Exception("Failed to load album: " + str(e))

        html = resp.text
        found_on_page = []

        # 1. Full https URLs to yupoo CDN
        for m in re.findall(r'https?://(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+', html):
            m = m.rstrip('\\/"\'')
            if re.search(r'\.(jpg|jpeg|png|webp)', m, re.I) and m not in all_urls:
                found_on_page.append(m)

        # 2. Protocol-relative URLs
        for m in re.findall(r'//(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+', html):
            full = 'https:' + m.rstrip('\\/"\'')
            if re.search(r'\.(jpg|jpeg|png|webp)', full, re.I) and full not in all_urls:
                found_on_page.append(full)

        # 3. Try to parse any embedded JSON state
        for match in re.finditer(r'(?:window\.__\w+__|var \w+)\s*=\s*(\{[\s\S]{20,}\})\s*;', html):
            try:
                data = json.loads(match.group(1))
                found = find_image_urls_in_json(data)
                for u in found:
                    if u not in all_urls and u not in found_on_page:
                        found_on_page.append(u)
            except Exception:
                pass

        # 4. Photo paths pattern: /username/albumid/photofile.jpg
        for m in re.findall(r'"(/[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+\.(?:jpg|jpeg|png|webp))"', html, re.I):
            for cdn in ['https://photo.yupoo.com', 'https://img.yupoo.com']:
                candidate = cdn + m
                if candidate not in all_urls and candidate not in found_on_page:
                    found_on_page.append(candidate)

        all_urls.extend(found_on_page)
        jobs[job_id]['message'] = 'Scraped ' + str(len(all_urls)) + ' image references (page ' + str(page) + ')...'

        # Detect next page
        if not found_on_page:
            break
        has_next = bool(re.search(r'page=' + str(page + 1), html))
        if not has_next:
            break
        page += 1
        time.sleep(0.5)

    return filter_and_upgrade_urls(all_urls)


def verify_images(session, subdomain, candidate_urls, job_id):
    """Filter out non-working URLs by doing a quick HEAD check on a sample."""
    if not candidate_urls:
        return []

    img_headers = {
        "Referer": "https://" + subdomain + "/",
        "Accept": "image/*,*/*;q=0.8",
    }

    verified = []
    jobs[job_id]['message'] = 'Verifying ' + str(len(candidate_urls)) + ' image URLs...'

    for url in candidate_urls:
        try:
            r = session.head(url, headers=img_headers, timeout=8)
            ct = r.headers.get('content-type', '')
            if r.status_code == 200 and ('image' in ct or 'octet' in ct):
                verified.append(url)
        except Exception:
            # Include anyway if we can't verify — download step will handle failures
            verified.append(url)

    return verified if verified else candidate_urls


def get_all_image_urls(subdomain, album_id, job_id):
    jobs[job_id]['status'] = 'fetching'
    jobs[job_id]['message'] = 'Starting session...'

    session = make_session(subdomain)

    # Try API endpoints first (fastest)
    jobs[job_id]['message'] = 'Trying Yupoo internal API...'
    urls = try_api_endpoints(session, subdomain, album_id, job_id)

    if not urls:
        # Fall back to HTML scraping with browser-level TLS
        jobs[job_id]['message'] = 'API returned nothing. Scraping album HTML...'
        urls = scrape_html_for_images(session, subdomain, album_id, job_id)

    return urls


def download_and_zip(job_id, subdomain, album_id, image_urls):
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

    session = cf_requests.Session(impersonate=IMPERSONATE)

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
    jobs[job_id]['zip_name'] = 'yupoo_album_' + album_id + '.zip'


def run_job(job_id, url):
    try:
        subdomain, album_id = extract_album_info(url)
        jobs[job_id]['album_id'] = album_id
        jobs[job_id]['subdomain'] = subdomain

        image_urls = get_all_image_urls(subdomain, album_id, job_id)

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found. The album may be private or Yupoo changed their site structure.'
            return

        jobs[job_id]['message'] = 'Found ' + str(len(image_urls)) + ' images. Starting download...'
        download_and_zip(job_id, subdomain, album_id, image_urls)

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
    }

    thread = threading.Thread(target=run_job, args=(job_id, url))
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
