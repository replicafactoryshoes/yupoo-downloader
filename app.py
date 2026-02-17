import os
import re
import io
import time
import zipfile
import threading
import requests
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify, send_file
from bs4 import BeautifulSoup

app = Flask(__name__)

jobs = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def extract_album_info(url):
    parsed = urlparse(url)
    subdomain = parsed.hostname
    match = re.search(r'/albums/(\d+)', parsed.path)
    if not match:
        raise ValueError("Could not extract album ID. Use a direct album link like: https://store.x.yupoo.com/albums/123456")
    album_id = match.group(1)
    return subdomain, album_id


def get_image_urls(subdomain, album_id, job_id):
    all_image_urls = []
    base_url = "https://" + subdomain
    page = 1

    jobs[job_id]['status'] = 'fetching'
    jobs[job_id]['message'] = 'Fetching album page...'

    session = requests.Session()
    session.headers.update(HEADERS)

    # First visit the main store page to get cookies
    try:
        session.get(base_url, timeout=15)
        time.sleep(1)
    except Exception:
        pass

    while True:
        url = base_url + "/albums/" + album_id
        params = {"uid": "1", "page": page}

        try:
            resp = session.get(url, params=params, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            raise Exception("Failed to load album page: " + str(e))

        soup = BeautifulSoup(resp.text, 'html.parser')
        found_on_page = []

        # Method 1: look for image tags inside photo containers
        for img in soup.find_all('img'):
            src = (
                img.get('src') or
                img.get('data-src') or
                img.get('data-original') or
                img.get('data-lazy') or
                ''
            )
            src = src.strip()
            if not src:
                continue
            if src.startswith('//'):
                src = 'https:' + src
            if ('photo.yupoo.com' in src or 'img.yupoo.com' in src) and src not in all_image_urls:
                found_on_page.append(src)

        # Method 2: look for URLs inside inline JSON / JS variables in script tags
        for script in soup.find_all('script'):
            content = script.string or ''
            matches = re.findall(
                r'(https?://(?:photo|img)\.yupoo\.com/[^\s"\'\\]+\.(?:jpg|jpeg|png|webp))',
                content, re.IGNORECASE
            )
            for m in matches:
                if m not in all_image_urls and m not in found_on_page:
                    found_on_page.append(m)

            matches2 = re.findall(
                r'(//(?:photo|img)\.yupoo\.com/[^\s"\'\\]+\.(?:jpg|jpeg|png|webp))',
                content, re.IGNORECASE
            )
            for m in matches2:
                full = 'https:' + m
                if full not in all_image_urls and full not in found_on_page:
                    found_on_page.append(full)

        all_image_urls.extend(found_on_page)
        jobs[job_id]['message'] = 'Found ' + str(len(all_image_urls)) + ' images so far (page ' + str(page) + ')...'

        # Check if there is a next page
        has_next = False
        next_link = soup.find('a', class_=re.compile(r'next', re.I))
        if not next_link:
            pager = soup.find(class_=re.compile(r'pag', re.I))
            if pager:
                links = pager.find_all('a', href=True)
                for lnk in links:
                    if str(page + 1) in lnk.get_text():
                        has_next = True
                        break

        if next_link:
            has_next = True

        if not has_next or not found_on_page:
            break

        page += 1
        time.sleep(0.8)

    # If we got nothing from HTML img tags, try fetching individual photo pages
    if not all_image_urls:
        jobs[job_id]['message'] = 'Trying photo detail pages...'
        all_image_urls = get_images_from_photo_pages(session, subdomain, album_id, job_id)

    return all_image_urls


def get_images_from_photo_pages(session, subdomain, album_id, job_id):
    all_image_urls = []
    base_url = "https://" + subdomain
    url = base_url + "/albums/" + album_id
    params = {"uid": "1"}

    try:
        resp = session.get(url, params=params, timeout=20)
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception:
        return []

    photo_links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/photos/' in href:
            if href.startswith('/'):
                href = base_url + href
            if href not in photo_links:
                photo_links.append(href)

    jobs[job_id]['message'] = 'Found ' + str(len(photo_links)) + ' photo pages, extracting images...'

    for i, photo_url in enumerate(photo_links):
        jobs[job_id]['message'] = 'Extracting image ' + str(i + 1) + ' of ' + str(len(photo_links)) + '...'
        try:
            r = session.get(photo_url, timeout=15)
            psoup = BeautifulSoup(r.text, 'html.parser')

            for img in psoup.find_all('img'):
                src = img.get('src') or img.get('data-src') or ''
                if src.startswith('//'):
                    src = 'https:' + src
                if ('photo.yupoo.com' in src or 'img.yupoo.com' in src) and src not in all_image_urls:
                    all_image_urls.append(src)
                    break

            time.sleep(0.3)
        except Exception:
            continue

    return all_image_urls


def download_and_zip(job_id, subdomain, album_id, image_urls):
    zip_buffer = io.BytesIO()
    total = len(image_urls)
    downloaded = 0
    failed = 0

    jobs[job_id]['status'] = 'downloading'
    jobs[job_id]['total'] = total

    img_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://" + subdomain + "/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, url in enumerate(image_urls):
            jobs[job_id]['message'] = 'Downloading image ' + str(i + 1) + ' of ' + str(total) + '...'
            jobs[job_id]['downloaded'] = downloaded

            try:
                resp = requests.get(url, headers=img_headers, timeout=30)
                resp.raise_for_status()

                content_type = resp.headers.get('Content-Type', '')
                ext = '.jpg'
                if 'png' in content_type:
                    ext = '.png'
                elif 'webp' in content_type:
                    ext = '.webp'
                elif 'gif' in content_type:
                    ext = '.gif'
                else:
                    url_ext = re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', url, re.I)
                    if url_ext:
                        ext = '.' + url_ext.group(1).lower()

                filename = "image_" + str(i + 1).zfill(4) + ext
                zf.writestr(filename, resp.content)
                downloaded += 1
            except Exception as e:
                failed += 1
                print("Failed to download " + url + ": " + str(e))

            time.sleep(0.15)

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

        image_urls = get_image_urls(subdomain, album_id, job_id)

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found. Make sure the URL is a direct album link (containing /albums/) and not a category or store page.'
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

    zip_data = job['zip_data']
    zip_name = job.get('zip_name', 'yupoo_album.zip')

    return send_file(
        io.BytesIO(zip_data),
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_name
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
