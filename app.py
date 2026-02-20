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
MAX_RETRIES = 3          # retries per image before giving up
RETRY_DELAY = 2.0        # seconds to wait between retries
DOWNLOAD_DELAY = 0.3     # seconds between successful downloads (rate limiting)


def make_session(subdomain):
    session = cf_requests.Session(impersonate=IMPERSONATE)
    try:
        # Visit store homepage AND an album page to build up cookies
        session.get("https://" + subdomain, timeout=15)
        time.sleep(0.5)
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
    parts = url.replace('https://', '').replace('http://', '').split('/')
    if len(parts) >= 3:
        return parts[1] + '/' + parts[2]
    return url


def pick_largest_per_photo(session, subdomain, candidate_urls, job_id):
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
    base = "https://" + subdomain
    all_urls = []

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

        for m in re.findall(r'https?://(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+', html):
            m = m.rstrip('\\/"\' ')
            if re.search(r'\.(jpg|jpeg|png|webp)', m, re.I) and m not in all_urls and m not in found_on_page:
                found_on_page.append(m)

        for m in re.findall(r'//(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+', html):
            full = 'https:' + m.rstrip('\\/"\' ')
            if re.search(r'\.(jpg|jpeg|png|webp)', full, re.I) and full not in all_urls and full not in found_on_page:
                found_on_page.append(full)

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

    candidates = collect_all_candidate_urls(session, subdomain, album_id, job_id)

    if not candidates:
        return [], session

    best = pick_largest_per_photo(session, subdomain, candidates, job_id)
    return best, session


def download_single_image(session, url, img_headers):
    """Download one image with retry logic. Returns bytes or raises."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                time.sleep(RETRY_DELAY * attempt)  # 2s, 4s backoff
            resp = session.get(url, headers=img_headers, timeout=40)
            if resp.status_code == 403 or resp.status_code == 429:
                # Rate limited or blocked — wait longer before retry
                time.sleep(3 + attempt * 2)
                continue
            resp.raise_for_status()
            if len(resp.content) < 500:
                # Too small to be a real image — probably an error page
                last_error = "Response too small (" + str(len(resp.content)) + " bytes)"
                continue
            return resp
        except Exception as e:
            last_error = str(e)
    raise Exception("Failed after " + str(MAX_RETRIES) + " attempts: " + str(last_error))


def download_and_zip(job_id, subdomain, album_id, image_urls, session, zip_name=None):
    zip_buffer = io.BytesIO()
    total = len(image_urls)
    downloaded = 0
    failed = 0
    failed_urls = []

    jobs[job_id]['status'] = 'downloading'
    jobs[job_id]['total'] = total

    # Use the SAME session that scraped (has cookies) + correct headers
    img_headers = {
        "Referer": "https://" + subdomain + "/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, url in enumerate(image_urls):
            jobs[job_id]['message'] = 'Downloading image ' + str(i + 1) + ' of ' + str(total) + '...'
            jobs[job_id]['downloaded'] = downloaded
            try:
                resp = download_single_image(session, url, img_headers)

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
                time.sleep(DOWNLOAD_DELAY)

            except Exception as e:
                failed += 1
                failed_urls.append(url)
                print("Failed: " + url + " -> " + str(e))

    zip_buffer.seek(0)
    jobs[job_id]['status'] = 'done'
    jobs[job_id]['downloaded'] = downloaded
    jobs[job_id]['failed'] = failed
    jobs[job_id]['failed_urls'] = failed_urls
    jobs[job_id]['message'] = 'Done! ' + str(downloaded) + ' downloaded, ' + str(failed) + ' failed.'
    jobs[job_id]['zip_data'] = zip_buffer.getvalue()
    jobs[job_id]['zip_name'] = zip_name or ('yupoo_album_' + album_id + '.zip')


def run_job(job_id, url, zip_name=None):
    try:
        subdomain, album_id = extract_album_info(url)
        jobs[job_id]['album_id'] = album_id
        jobs[job_id]['subdomain'] = subdomain
        jobs[job_id]['original_url'] = url
        jobs[job_id]['original_zip_name'] = zip_name

        image_urls, session = get_all_image_urls(subdomain, album_id, job_id)
        jobs[job_id]['raw_urls'] = image_urls
        jobs[job_id]['session'] = session  # keep session alive for retry

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found. The album may be private or Yupoo changed their structure.'
            return

        jobs[job_id]['message'] = 'Found ' + str(len(image_urls)) + ' unique photos. Starting download...'

        custom_name = (zip_name or '').strip()
        if custom_name:
            safe = re.sub(r'[<>:"/\\|?*]', '', custom_name).strip()
            final_zip_name = safe + '.zip'
        else:
            final_zip_name = 'yupoo_album_' + album_id + '.zip'

        jobs[job_id]['final_zip_name'] = final_zip_name
        download_and_zip(job_id, subdomain, album_id, image_urls, session, final_zip_name)

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['message'] = 'Error: ' + str(e)


def retry_job(job_id):
    """Re-run only the failed images from a completed job."""
    job = jobs.get(job_id)
    if not job:
        return

    failed_urls = job.get('failed_urls', [])
    subdomain = job.get('subdomain', '')
    album_id = job.get('album_id', '')
    session = job.get('session')
    zip_name = job.get('final_zip_name')

    if not failed_urls:
        # No specific failures recorded — retry the whole job
        original_url = job.get('original_url', '')
        original_zip_name = job.get('original_zip_name', '')
        # Reset job state
        job.update({
            'status': 'starting', 'message': 'Retrying...', 'downloaded': 0,
            'total': 0, 'failed': 0, 'zip_data': None, 'raw_urls': [], 'failed_urls': []
        })
        thread = threading.Thread(target=run_job, args=(job_id, original_url, original_zip_name))
        thread.daemon = True
        thread.start()
        return

    # Retry only failed images — merge with previously downloaded ones
    prev_zip_data = job.get('zip_data')
    prev_downloaded = job.get('downloaded', 0)

    job.update({
        'status': 'downloading',
        'message': 'Retrying ' + str(len(failed_urls)) + ' failed images...',
        'failed': 0,
        'failed_urls': [],
    })

    def do_retry():
        nonlocal prev_zip_data
        try:
            # Use same session or create new one
            sess = session or make_session(subdomain)

            img_headers = {
                "Referer": "https://" + subdomain + "/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "cross-site",
            }

            # Start a new zip, copying existing data then appending retried images
            new_zip_buffer = io.BytesIO()
            newly_downloaded = 0
            still_failed = []
            still_failed_urls = []

            # Count existing images in previous zip
            existing_count = prev_downloaded

            with zipfile.ZipFile(new_zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as new_zf:
                # Copy previously successful images
                if prev_zip_data:
                    old_buf = io.BytesIO(prev_zip_data)
                    with zipfile.ZipFile(old_buf, 'r') as old_zf:
                        for name in old_zf.namelist():
                            new_zf.writestr(name, old_zf.read(name))

                # Retry failed ones
                for i, url in enumerate(failed_urls):
                    job['message'] = 'Retry: image ' + str(i + 1) + ' of ' + str(len(failed_urls)) + '...'
                    try:
                        resp = download_single_image(sess, url, img_headers)
                        content_type = resp.headers.get('content-type', '')
                        ext = '.jpg'
                        if 'png' in content_type:
                            ext = '.png'
                        elif 'webp' in content_type:
                            ext = '.webp'
                        m = re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', url, re.I)
                        if m:
                            ext = '.' + m.group(1).lower()
                        filename = "image_retry_" + str(existing_count + i + 1).zfill(4) + ext
                        new_zf.writestr(filename, resp.content)
                        newly_downloaded += 1
                        time.sleep(DOWNLOAD_DELAY)
                    except Exception as e:
                        still_failed += 1
                        still_failed_urls.append(url)
                        print("Retry failed: " + url + " -> " + str(e))

            new_zip_buffer.seek(0)
            total_downloaded = existing_count + newly_downloaded
            job['status'] = 'done'
            job['downloaded'] = total_downloaded
            job['failed'] = len(still_failed_urls)
            job['failed_urls'] = still_failed_urls
            job['zip_data'] = new_zip_buffer.getvalue()
            job['message'] = 'Retry done! ' + str(total_downloaded) + ' total, ' + str(len(still_failed_urls)) + ' still failed.'

        except Exception as e:
            job['status'] = 'done'
            job['message'] = 'Retry error: ' + str(e)

    thread = threading.Thread(target=do_retry)
    thread.daemon = True
    thread.start()


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
        'failed_urls': [],
        'zip_data': None,
        'raw_urls': [],
    }

    thread = threading.Thread(target=run_job, args=(job_id, url, zip_name))
    thread.daemon = True
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/retry/<job_id>', methods=['POST'])
def retry_download(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') not in ('done', 'error'):
        return jsonify({'error': 'Job is still running'}), 400
    retry_job(job_id)
    return jsonify({'ok': True})


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
