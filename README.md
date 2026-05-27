# Überböse Web 🔈🎶

A lightweight, high-performance web controller designed to orchestrate Bose SoundTouch hardware networks. By communicating directly with local speaker endpoints, this service offers real-time telemetry streaming, advanced developer utilities, and direct preset injection.

⚠️ **Prerequisite:** This frontend web application requires [ueberboese-api](https://github.com/julius-d/ueberboese-api) running as its backend management server.

### Key Features

* 📺 **Real-time Dashboard:** Alphabetical overview of all audio nodes with reactive Server-Sent Events (SSE) telemetry.
* ➕ **Hardware Preset Injector:** Live lookup engine to bind TuneIn stations and Spotify containers directly to physical button slots (1-6).
* 👨‍⚕️ **Speaker Doctor:** Low-level TCP socket bridge via port 17000 to read hardware configuration registers and trigger remote hard reboots.
* 🛡️ **Browser & Mobile Optimized:** Built with pure CSS components ensuring full UI rendering across mobile browsers.
* 🌐 **Multi-Language Support:** Fully localized in English (EN) and Dutch (NL) using Flask-Babel.

---

## 🛠️ Environment Variables

The application utilizes environment variables for configuration. Create a `.env` file in the root directory based on the following keys:


```bash
# Example .env configuration
FLASK_SECRET_KEY=your_super_secret_session_key_here
SPOTIFY_CLIENT_ID=your_spotify_client_id_here
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret_here
UEBERBOESE_API_URL=http://10.0.0.5:8000
UEBERBOESE_WEB_PORT=7082

```

| Variable | Default Value | Description |
| --- | --- | --- |
| `FLASK_SECRET_KEY` | *Required* | A unique, secure key used by Flask to sign session cookies (required to persist user language preferences). |
| `SPOTIFY_CLIENT_ID` | *Optional* | The Client ID from your Spotify Developer Dashboard. |
| `SPOTIFY_CLIENT_SECRET` | *Optional* | The Client Secret from your Spotify Developer Dashboard. |
| `UEBERBOESE_API_URL` | `http://10.0.0.5:8000` | The endpoint URL where your back-end Überböse API management engine is running. |
| `UEBERBOESE_WEB_PORT` | `7082` | The custom networking port the Flask web server will bind to inside the container. |

---

## 🚀 Running the Application

### Method A: With Docker Compose (Recommended)

Launch the container ecosystem instantly using the configured settings:

```bash
docker-compose up -d --build

```

### Method B: Native Docker Commands

To assemble and execute the container standalone, run:

```bash
# Build the application image
docker build -t ueberboese-web:latest .

# Launch using local host network mode with your environmental configurations attached
docker run -d \\
  --name ueberboese-web \\
  --network host \\
  --env-file .env \\
  --restart unless-stopped \\
  ueberboese-web:latest

```
