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
from bs4 import BeautifulSoup

app = Flask(__name__)
jobs = {}

SESSION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def extract_album_info(url):
    parsed = urlparse(url)
    subdomain = parsed.hostname
    match = re.search(r'/albums/(\d+)', parsed.path)
    if not match:
        raise ValueError("Could not find album ID in URL. Make sure the link contains /albums/")
    album_id = match.group(1)
    return subdomain, album_id


def make_session(subdomain):
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    # Visit homepage first to get cookies
    try:
        session.get("https://" + subdomain, timeout=15)
        time.sleep(0.5)
    except Exception:
        pass
    return session


def extract_images_from_page(html, subdomain):
    """Try every known method to extract image URLs from a Yupoo page."""
    images = []

    # ----------------------------------------------------------------
    # Method 1: window.__INITIAL_STATE__ or similar embedded JSON blobs
    # ----------------------------------------------------------------
    json_patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*({.+?})(?:;|\n|</script>)',
        r'window\.__STORE__\s*=\s*({.+?})(?:;|\n|</script>)',
        r'window\.pageData\s*=\s*({.+?})(?:;|\n|</script>)',
        r'var\s+pageData\s*=\s*({.+?})(?:;|\n|</script>)',
    ]
    for pattern in json_patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                found = find_image_urls_in_json(data)
                images.extend([u for u in found if u not in images])
            except Exception:
                pass

    # ----------------------------------------------------------------
    # Method 2: All JSON-like objects inside script tags
    # ----------------------------------------------------------------
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script'):
        content = script.string or ''
        if not content:
            continue

        # Look for any JSON array or object that might contain image data
        # Find all quoted strings that look like yupoo image paths
        raw_paths = re.findall(
            r'"((?:[a-zA-Z0-9/_\-]+/)+[a-zA-Z0-9/_\-]+\.(?:jpg|jpeg|png|webp))"',
            content, re.IGNORECASE
        )
        for path in raw_paths:
            # Yupoo image paths look like: /xxx/xxx/filename.jpg
            candidates = [
                "https://photo.yupoo.com" + path,
                "https://img.yupoo.com" + path,
            ]
            for c in candidates:
                if c not in images:
                    images.append(c)

        # Direct full URLs in scripts
        full_urls = re.findall(
            r'https?://(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+\.(?:jpg|jpeg|png|webp)[^\s"\'<>\\]*',
            content, re.IGNORECASE
        )
        for u in full_urls:
            u = u.rstrip('\\/')
            if u not in images:
                images.append(u)

        # Protocol-relative URLs in scripts
        rel_urls = re.findall(
            r'//(?:photo|img)\.yupoo\.com/[^\s"\'<>\\]+\.(?:jpg|jpeg|png|webp)[^\s"\'<>\\]*',
            content, re.IGNORECASE
        )
        for u in rel_urls:
            full = 'https:' + u.rstrip('\\/')
            if full not in images:
                images.append(full)

    # ----------------------------------------------------------------
    # Method 3: img tags (works when JS rendering is not needed)
    # ----------------------------------------------------------------
    for img in soup.find_all('img'):
        for attr in ['src', 'data-src', 'data-original', 'data-lazy', 'data-url']:
            src = img.get(attr, '').strip()
            if not src:
                continue
            if src.startswith('//'):
                src = 'https:' + src
            if ('photo.yupoo.com' in src or 'img.yupoo.com' in src) and src not in images:
                images.append(src)

    # ----------------------------------------------------------------
    # Method 4: Any URL in the raw HTML matching yupoo image CDN
    # ----------------------------------------------------------------
    all_cdn = re.findall(
        r'https?://(?:photo|img)\.yupoo\.com/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)',
        html, re.IGNORECASE
    )
    for u in all_cdn:
        u = u.strip()
        if u not in images:
            images.append(u)

    rel_cdn = re.findall(
        r'//(?:photo|img)\.yupoo\.com/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)',
        html, re.IGNORECASE
    )
    for u in rel_cdn:
        full = 'https:' + u.strip()
        if full not in images:
            images.append(full)

    return images


def find_image_urls_in_json(obj, found=None):
    """Recursively walk a JSON object and collect image URLs."""
    if found is None:
        found = []
    if isinstance(obj, str):
        if re.search(r'(?:photo|img)\.yupoo\.com', obj):
            url = obj if obj.startswith('http') else 'https:' + obj
            if url not in found:
                found.append(url)
        elif re.search(r'\.(?:jpg|jpeg|png|webp)$', obj, re.I) and '/' in obj:
            for prefix in ['https://photo.yupoo.com', 'https://img.yupoo.com']:
                candidate = prefix + (obj if obj.startswith('/') else '/' + obj)
                if candidate not in found:
                    found.append(candidate)
    elif isinstance(obj, dict):
        for v in obj.values():
            find_image_urls_in_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_image_urls_in_json(item, found)
    return found


def get_photo_ids_from_album(session, subdomain, album_id, job_id):
    """Get all photo page URLs from the album listing."""
    base_url = "https://" + subdomain
    photo_links = []
    page = 1

    while True:
        url = base_url + "/albums/" + album_id
        params = {"uid": "1", "page": page}
        try:
            resp = session.get(url, params=params, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            raise Exception("Failed to load album: " + str(e))

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Collect all links to individual photo pages
        found_on_page = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/photos/' in href:
                if href.startswith('/'):
                    href = base_url + href
                elif not href.startswith('http'):
                    href = base_url + '/' + href
                if href not in photo_links:
                    found_on_page.append(href)
                    photo_links.append(href)

        jobs[job_id]['message'] = 'Found ' + str(len(photo_links)) + ' photos so far (scanning page ' + str(page) + ')...'

        # Detect next page
        has_next = False
        # Check for a "next" button/link
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True).lower()
            classes = ' '.join(a.get('class', []))
            if 'next' in text or 'next' in classes.lower():
                has_next = True
                break
        # Also check if there's a page number higher than current
        page_nums = re.findall(r'[?&]page=(\d+)', resp.text)
        if page_nums:
            max_page = max(int(p) for p in page_nums)
            if max_page > page:
                has_next = True

        if not has_next or not found_on_page:
            break

        page += 1
        time.sleep(0.5)

    return photo_links


def get_image_from_photo_page(session, photo_url, subdomain):
    """Fetch a single photo page and extract the full-size image URL."""
    try:
        resp = session.get(photo_url, timeout=15)
        images = extract_images_from_page(resp.text, subdomain)
        # Return the first (and usually only) image found
        if images:
            return images[0]
    except Exception:
        pass
    return None


def get_all_image_urls(subdomain, album_id, job_id):
    session = make_session(subdomain)
    base_url = "https://" + subdomain

    jobs[job_id]['status'] = 'fetching'
    jobs[job_id]['message'] = 'Loading album page...'

    # Step 1: Load the album page and try to extract images directly
    album_url = base_url + "/albums/" + album_id
    try:
        resp = session.get(album_url, params={"uid": "1"}, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise Exception("Could not load the album page: " + str(e))

    direct_images = extract_images_from_page(resp.text, subdomain)

    if direct_images:
        jobs[job_id]['message'] = 'Found ' + str(len(direct_images)) + ' images directly on album page!'
        # Check if there are more pages
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Look for pagination to get more pages
        page = 2
        while True:
            has_more = False
            for a in soup.find_all('a', href=True):
                if 'page=' + str(page) in a.get('href', '') or a.get_text(strip=True).lower() == 'next':
                    has_more = True
                    break
            if not has_more:
                break
            try:
                resp2 = session.get(album_url, params={"uid": "1", "page": page}, timeout=20)
                more = extract_images_from_page(resp2.text, subdomain)
                new = [u for u in more if u not in direct_images]
                direct_images.extend(new)
                jobs[job_id]['message'] = 'Found ' + str(len(direct_images)) + ' images (page ' + str(page) + ')...'
                soup = BeautifulSoup(resp2.text, 'html.parser')
                page += 1
                time.sleep(0.5)
            except Exception:
                break
        return direct_images

    # Step 2: If no images found directly, get individual photo page URLs and scrape each
    jobs[job_id]['message'] = 'Album uses dynamic loading. Collecting photo pages...'
    photo_links = get_photo_ids_from_album(session, subdomain, album_id, job_id)

    if not photo_links:
        return []

    all_images = []
    total = len(photo_links)
    for i, link in enumerate(photo_links):
        jobs[job_id]['message'] = 'Extracting image ' + str(i + 1) + ' of ' + str(total) + '...'
        img_url = get_image_from_photo_page(session, link, subdomain)
        if img_url and img_url not in all_images:
            all_images.append(img_url)
        time.sleep(0.2)

    return all_images


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

        image_urls = get_all_image_urls(subdomain, album_id, job_id)

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found. The album may be private, empty, or fully JavaScript-rendered. Try a different album link.'
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
