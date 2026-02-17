import os
import re
import io
import time
import zipfile
import threading
import requests
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

jobs = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.yupoo.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def extract_album_info(url):
    parsed = urlparse(url)
    subdomain = parsed.hostname
    match = re.search(r'/albums/(\d+)', parsed.path)
    if not match:
        raise ValueError("Could not extract album ID from URL. Make sure it is a direct album link like: https://store.x.yupoo.com/albums/123456")
    album_id = match.group(1)
    return subdomain, album_id


def get_image_urls(subdomain, album_id, job_id):
    base_url = "https://" + subdomain
    all_image_urls = []
    page = 1
    page_size = 30

    jobs[job_id]['status'] = 'fetching'
    jobs[job_id]['message'] = 'Fetching album info...'

    while True:
        api_url = base_url + "/api/albums/" + album_id + "/photos"
        params = {
            "uid": "1",
            "page": page,
            "pageSize": page_size,
        }

        try:
            resp = requests.get(api_url, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.JSONDecodeError:
            jobs[job_id]['message'] = 'API not available, trying HTML scraping...'
            return scrape_html_for_images(subdomain, album_id, job_id)
        except Exception as e:
            raise Exception("Failed to fetch album data: " + str(e))

        photos = data.get('photos', data.get('data', {}).get('photos', []))

        if not photos:
            if isinstance(data, list):
                photos = data
            elif 'data' in data:
                photos = data['data']
            else:
                break

        if not photos:
            break

        for photo in photos:
            img_url = (
                photo.get('path') or
                photo.get('url') or
                photo.get('src') or
                photo.get('imageUrl') or
                photo.get('image_url')
            )
            if img_url:
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                elif img_url.startswith('/'):
                    img_url = "https://" + subdomain + img_url
                all_image_urls.append(img_url)

        jobs[job_id]['message'] = 'Found ' + str(len(all_image_urls)) + ' images so far...'

        total = data.get('total', data.get('data', {}).get('total', 0))
        if not total or len(all_image_urls) >= total or len(photos) < page_size:
            break
        page += 1
        time.sleep(0.3)

    return all_image_urls


def scrape_html_for_images(subdomain, album_id, job_id):
    from bs4 import BeautifulSoup

    all_urls = []
    base_url = "https://" + subdomain

    jobs[job_id]['message'] = 'Scraping album HTML page...'

    page = 1
    while True:
        url = base_url + "/albums/" + album_id
        params = {"uid": "1", "page": page}

        headers = dict(HEADERS)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

        resp = requests.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')

        found = False

        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src') or img.get('data-original') or ''
            if 'yupoo.com' in src or 'photo' in src.lower():
                if src.startswith('//'):
                    src = 'https:' + src
                if src not in all_urls:
                    all_urls.append(src)
                    found = True

        for script in soup.find_all('script'):
            content = script.string or ''
            urls_in_script = re.findall(r'https?://[^\s"\']+(?:jpg|jpeg|png|webp)', content, re.IGNORECASE)
            for u in urls_in_script:
                if 'photo' in u.lower() or 'yupoo' in u.lower():
                    if u not in all_urls:
                        all_urls.append(u)
                        found = True

        jobs[job_id]['message'] = 'Scraped ' + str(len(all_urls)) + ' images from page ' + str(page) + '...'

        next_btn = soup.find('a', string=re.compile(r'next', re.I))
        if not next_btn or not found:
            break
        page += 1
        time.sleep(0.5)

    return all_urls


def download_and_zip(job_id, subdomain, album_id, image_urls):
    zip_buffer = io.BytesIO()
    total = len(image_urls)
    downloaded = 0
    failed = 0

    jobs[job_id]['status'] = 'downloading'
    jobs[job_id]['total'] = total

    img_headers = dict(HEADERS)
    img_headers['Referer'] = "https://" + subdomain + "/"
    img_headers['Accept'] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

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

        image_urls = get_image_urls(subdomain, album_id, job_id)

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found in this album. Make sure the URL is a direct album link and not a category page.'
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
