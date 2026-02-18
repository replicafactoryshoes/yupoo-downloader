import os
import re
import io
import json
import time
import zipfile
import threading
import requests
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify, send_file
from playwright.sync_api import sync_playwright

app = Flask(__name__)
jobs = {}


def extract_album_info(url):
    parsed = urlparse(url)
    subdomain = parsed.hostname
    match = re.search(r'/albums/(\d+)', parsed.path)
    if not match:
        raise ValueError("Could not find album ID in URL. Make sure the link contains /albums/")
    album_id = match.group(1)
    return subdomain, album_id


def scrape_with_playwright(subdomain, album_id, job_id):
    """Use a real headless browser to load the page and extract image URLs."""
    base_url = "https://" + subdomain
    album_url = base_url + "/albums/" + album_id + "?uid=1"
    all_image_urls = []

    jobs[job_id]['message'] = 'Launching headless browser...'

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # Intercept network responses to capture image URLs directly
        intercepted_images = []

        def handle_response(response):
            url = response.url
            content_type = response.headers.get('content-type', '')
            if any(x in url for x in ['photo.yupoo.com', 'img.yupoo.com']):
                if any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    if url not in intercepted_images:
                        intercepted_images.append(url)
            # Also watch for JSON API responses that list photos
            if 'application/json' in content_type and ('photo' in url or 'album' in url):
                try:
                    body = response.json()
                    found = find_image_urls_in_json(body)
                    for u in found:
                        if u not in intercepted_images:
                            intercepted_images.append(u)
                except Exception:
                    pass

        page.on('response', handle_response)

        jobs[job_id]['message'] = 'Loading album page in browser...'
        page.goto(album_url, wait_until='networkidle', timeout=60000)

        # Scroll down to trigger lazy loading
        jobs[job_id]['message'] = 'Scrolling page to load all images...'
        for _ in range(10):
            page.evaluate("window.scrollBy(0, 800)")
            time.sleep(0.4)

        page.wait_for_timeout(2000)

        # Extract from rendered DOM
        html = page.content()

        # Also grab all img src from the live DOM
        img_srcs = page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img');
                return Array.from(imgs).map(img => img.src || img.getAttribute('data-src') || '').filter(Boolean);
            }
        """)

        for src in img_srcs:
            if src.startswith('//'):
                src = 'https:' + src
            if ('photo.yupoo.com' in src or 'img.yupoo.com' in src) and src not in all_image_urls:
                all_image_urls.append(src)

        # Add intercepted images
        for u in intercepted_images:
            if u not in all_image_urls:
                all_image_urls.append(u)

        jobs[job_id]['message'] = 'Found ' + str(len(all_image_urls)) + ' images on page 1. Checking for more pages...'

        # Check for pagination and handle multiple pages
        page_num = 2
        while True:
            # Look for a "next page" button
            next_btn = page.query_selector('a.next, a[class*="next"], .pagination a:last-child')
            if not next_btn:
                # Try by URL pattern - check if page=N links exist
                next_url = base_url + "/albums/" + album_id + "?uid=1&page=" + str(page_num)
                prev_count = len(all_image_urls)

                intercepted_images.clear()
                page.goto(next_url, wait_until='networkidle', timeout=60000)

                for _ in range(10):
                    page.evaluate("window.scrollBy(0, 800)")
                    time.sleep(0.4)
                page.wait_for_timeout(2000)

                new_srcs = page.evaluate("""
                    () => {
                        const imgs = document.querySelectorAll('img');
                        return Array.from(imgs).map(img => img.src || img.getAttribute('data-src') || '').filter(Boolean);
                    }
                """)
                new_found = 0
                for src in new_srcs:
                    if src.startswith('//'):
                        src = 'https:' + src
                    if ('photo.yupoo.com' in src or 'img.yupoo.com' in src) and src not in all_image_urls:
                        all_image_urls.append(src)
                        new_found += 1
                for u in intercepted_images:
                    if u not in all_image_urls:
                        all_image_urls.append(u)
                        new_found += 1

                jobs[job_id]['message'] = 'Found ' + str(len(all_image_urls)) + ' images total (page ' + str(page_num) + ')...'

                if new_found == 0:
                    break
                page_num += 1
            else:
                break

        browser.close()

    return all_image_urls


def find_image_urls_in_json(obj, found=None):
    if found is None:
        found = []
    if isinstance(obj, str):
        if re.search(r'(?:photo|img)\.yupoo\.com', obj):
            url = obj if obj.startswith('http') else 'https:' + obj
            if url not in found:
                found.append(url)
    elif isinstance(obj, dict):
        for v in obj.values():
            find_image_urls_in_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_image_urls_in_json(item, found)
    return found


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
                print("Failed: " + url + " -> " + str(e))

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

        image_urls = scrape_with_playwright(subdomain, album_id, job_id)

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found even after rendering the page. The album may be private or require login.'
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
