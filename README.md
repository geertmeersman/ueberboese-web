# ueberboese-web

A lightweight, high-performance web controller designed to orchestrate Bose SoundTouch hardware networks. By communicating directly with local speaker endpoints, this service offers real-time telemetry streaming, advanced developer utilities, and direct preset injection.

⚠️ **Prerequisite:** This frontend web application requires [ueberboese-api](https://github.com/julius-d/ueberboese-api) running as its backend management server.

### Key Features
* 📺 **Real-time Dashboard:** Alphabetical overview of all audio nodes with reactive Server-Sent Events (SSE) telemetry.
* ➕ **Hardware Preset Injector:** Live lookup engine to bind TuneIn stations and Spotify containers directly to physical button slots (1-6).
* 👨‍⚕️ **Speaker Doctor:** Low-level TCP socket bridge via port 17000 to read hardware configuration registers and trigger remote hard reboots.
* 🛡️ **Browser & Mobile Optimized:** Built with pure CSS components ensuring full UI rendering across mobile browsers.
