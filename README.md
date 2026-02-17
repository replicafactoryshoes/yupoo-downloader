# 📦 Yupoo Album Downloader

A local web app that downloads all images from any Yupoo album and saves them as a ZIP file.

---

## 🚀 Quick Start

### 1. Install Python requirements

```bash
pip install flask requests beautifulsoup4
```

### 2. Run the app

```bash
python app.py
```

### 3. Open in browser

Go to: **http://localhost:5000**

---

## 📋 How to Use

1. Open a Yupoo store and navigate to a specific product album
2. Copy the album URL from your browser — it must look like:
   ```
   https://storename.x.yupoo.com/albums/123456789?uid=1
   ```
3. Paste it into the input box and click **Download**
4. Wait for all images to be downloaded and zipped
5. Click **Save ZIP to Computer** to download

---

## ⚠️ Important: Use Album Links Only

| ✅ Correct | ❌ Wrong |
|---|---|
| `https://store.x.yupoo.com/albums/183703566` | `https://store.x.yupoo.com/categories` |
| `https://store.x.yupoo.com/albums/205649537?uid=1` | `https://store.x.yupoo.com/albums` |

---

## 🛠️ How It Works

1. Parses the album ID from the URL
2. Calls Yupoo's internal API to get all photo URLs (falls back to HTML scraping if API is unavailable)
3. Downloads each image with proper headers (Referer, User-Agent) that Yupoo requires
4. Compresses all images into a single `.zip` file in memory
5. Serves the ZIP file for download through the browser

---

## 📦 Output

- ZIP file named: `yupoo_album_<albumID>.zip`
- Images named: `image_0001.jpg`, `image_0002.jpg`, etc.

---

## Notes

- Yupoo requires a valid `Referer` header to serve images — this app handles that automatically
- Downloads are rate-limited slightly to avoid being blocked
- Large albums (100+ images) may take a minute or two
