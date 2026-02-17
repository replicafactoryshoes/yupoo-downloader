import os
import re
import io
import time
import zipfile
import threading
import requests
from urllib.parse import urlparse, urlencode
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

# Store download jobs in memory
jobs = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.yupoo.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

def extract_album_info(url):
    """Extract subdomain and album ID from a Yupoo album URL."""
    parsed = urlparse(url)
    # e.g. 0594tmall.x.yupoo.com
    subdomain = parsed.hostname  # e.g. 0594tmall.x.yupoo.com
    # Extract album ID from path like /albums/183703566
    match = re.search(r'/albums/(\d+)', parsed.path)
    if not match:
        raise ValueError("Could not extract album ID from URL. Make sure it's a direct album link like: https://store.x.yupoo.com/albums/123456")
    album_id = match.group(1)
    return subdomain, album_id

def get_image_urls(subdomain, album_id, job_id):
    """Fetch all image URLs from a Yupoo album using their API."""
    base_url = f"https://{subdomain}"
    all_image_urls = []
    page = 1
    page_size = 30

    jobs[job_id]['status'] = 'fetching'
    jobs[job_id]['message'] = 'Fetching album info...'

    while True:
        # Yupoo API endpoint for album photos
        api_url = f"{base_url}/api/albums/{album_id}/photos"
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
            # Fallback: try scraping the HTML page
            jobs[job_id]['message'] = 'API not available, trying HTML scraping...'
            return scrape_html_for_images(subdomain, album_id, job_id)
        except Exception as e:
            raise Exception(f"Failed to fetch album data: {str(e)}")

        photos = data.get('photos', data.get('data', {}).get('photos', []))
        
        if not photos:
            # Try alternative response structure
            if isinstance(data, list):
                photos = data
            elif 'data' in data:
                photos = data['data']
            else:
                break

        if not photos:
            break

        for photo in photos:
            # Extract image URL from various possible fields
            img_url = (
                photo.get('path') or
                photo.get('url') or
                photo.get('src') or
                photo.get('imageUrl') or
                photo.get('image_url')
            )
            if img_url:
                # Make sure URL is absolute
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                elif img_url.startswith('/'):
                    img_url = f"https://{subdomain}" + img_url
                all_image_urls.append(img_url)

        jobs[job_id]['message'] = f'Found {len(all_image_urls)} images so far...'

        # Check if there are more pages
        total = data.get('total', data.get('data', {}).get('total', 0))
        if not total or len(all_image_urls) >= total or len(photos) < page_size:
            break
        page += 1
        time.sleep(0.3)  # Be polite

    return all_image_urls

def scrape_html_for_images(subdomain, album_id, job_id):
    """Fallback: scrape the HTML page for image URLs."""
    from bs4 import BeautifulSoup

    all_urls = []
    base_url = f"https://{subdomain}"
    
    jobs[job_id]['message'] = 'Scraping album HTML page...'

    page = 1
    while True:
        url = f"{base_url}/albums/{album_id}"
        params = {"uid": "1", "page": page}
        
        headers = dict(HEADERS)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Find all image tags or photo containers
        found = False
        
        # Method 1: Look for img tags with yupoo photo domains
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src') or img.get('data-original') or ''
            if 'yupoo.com' in src or 'photo' in src.lower():
                if src.startswith('//'):
                    src = 'https:' + src
                if src not in all_urls:
                    all_urls.append(src)
                    found = True

        # Method 2: Look for photo links in JSON-like script tags
        for script in soup.find_all('script'):
            content = script.string or ''
            # Find image URLs in JS
            urls_in_script = re.findall(r'https?://[^\s"\']+(?:jpg|jpeg|png|webp)', content, re.IGNORECASE)
            for u in urls_in_script:
                if 'photo' in u.lower() or 'yupoo' in u.lower():
                    if u not in all_urls:
                        all_urls.append(u)
                        found = True

        jobs[job_id]['message'] = f'Scraped {len(all_urls)} images from page {page}...'

        # Check for next page
        next_btn = soup.find('a', string=re.compile(r'next|下一页', re.I))
        if not next_btn or not found:
            break
        page += 1
        time.sleep(0.5)

    return all_urls

def download_and_zip(job_id, subdomain, album_id, image_urls):
    """Download all images and compress them into a ZIP in memory."""
    zip_buffer = io.BytesIO()
    total = len(image_urls)
    downloaded = 0
    failed = 0

    jobs[job_id]['status'] = 'downloading'
    jobs[job_id]['total'] = total

    img_headers = dict(HEADERS)
    img_headers['Referer'] = f"https://{subdomain}/"
    img_headers['Accept'] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, url in enumerate(image_urls):
            jobs[job_id]['message'] = f'Downloading image {i+1} of {total}...'
            jobs[job_id]['downloaded'] = downloaded

            try:
                resp = requests.get(url, headers=img_headers, timeout=30)
                resp.raise_for_status()

                # Determine file extension
                content_type = resp.headers.get('Content-Type', '')
                ext = '.jpg'
                if 'png' in content_type:
                    ext = '.png'
                elif 'webp' in content_type:
                    ext = '.webp'
                elif 'gif' in content_type:
                    ext = '.gif'
                else:
                    # Try to get from URL
                    url_ext = re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', url, re.I)
                    if url_ext:
                        ext = '.' + url_ext.group(1).lower()

                filename = f"image_{i+1:04d}{ext}"
                zf.writestr(filename, resp.content)
                downloaded += 1
            except Exception as e:
                failed += 1
                print(f"Failed to download {url}: {e}")

            time.sleep(0.1)  # Rate limiting

    zip_buffer.seek(0)
    jobs[job_id]['status'] = 'done'
    jobs[job_id]['downloaded'] = downloaded
    jobs[job_id]['failed'] = failed
    jobs[job_id]['message'] = f'Done! Downloaded {downloaded} images, {failed} failed.'
    jobs[job_id]['zip_data'] = zip_buffer.getvalue()
    jobs[job_id]['zip_name'] = f"yupoo_album_{album_id}.zip"

def run_job(job_id, url):
    try:
        subdomain, album_id = extract_album_info(url)
        jobs[job_id]['album_id'] = album_id
        jobs[job_id]['subdomain'] = subdomain

        image_urls = get_image_urls(subdomain, album_id, job_id)

        if not image_urls:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['message'] = 'No images found in this album. Make sure the URL is a direct album link (not a category page).'
            return

        jobs[job_id]['message'] = f'Found {len(image_urls)} images. Starting download...'
        download_and_zip(job_id, subdomain, album_id, image_urls)

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['message'] = f'Error: {str(e)}'

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
    print("\n🖼️  Yupoo Album Downloader")
    print("=" * 40)
    print("Open your browser at: http://localhost:5000")
    print("=" * 40 + "\n")
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
```

(Render assigns a port automatically, so the app needs to read it from the environment.)

---

### Step 5 — Sign up on Render

Go to **https://render.com** → Sign up with your GitHub account (click "Sign up with GitHub" — easiest way)

---

### Step 6 — Deploy your app

1. On Render's dashboard, click **"New"** → **"Web Service"**
2. Connect your GitHub account if prompted
3. Find and select your `yupoo-downloader` repository
4. Fill in the settings:
   - **Name:** yupoo-downloader (or anything you like)
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python app.py`
5. Choose the **Free** plan
6. Click **"Create Web Service"**

---

### Step 7 — Wait ~2 minutes

Render will build and deploy your app. When it's done, you'll see a URL at the top like:
```
https://yupoo-downloader.onrender.com
