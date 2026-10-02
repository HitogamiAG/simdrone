const { chromium } = require('playwright');
const http = require('http');

const backend = process.env.BACKEND_URL || 'http://backend:8003';
const media = process.env.MEDIAMTX_URL || 'http://mediamtx:8889';

async function main() {
  const name = `browser_webrtc_${Date.now()}`;
  let droneId;
  let server;
  let browser;
  try {
    const created = await fetch(`${backend}/api/v1/drones/`, {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({model: 'x500_gimbal', name, pose: {
        position: {x: 0, y: 0, z: 3}, orientation: {x: 0, y: 0, z: 0, w: 1}
      }})
    });
    const payload = await created.json();
    if (!created.ok) throw new Error(`Backend create failed ${created.status}: ${JSON.stringify(payload)}`);
    droneId = payload.id;
    let sensor = payload.simulation.sensors.find((item) => item.type === 'camera');
    for (let attempt = 0; !sensor && attempt < 10; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const refreshed = await fetch(`${backend}/api/v1/drones/${droneId}/sensors/`);
      sensor = (await refreshed.json()).find((item) => item.type === 'camera');
    }
    if (!sensor) throw new Error(`Backend drone response contains no camera sensor: ${JSON.stringify(payload.simulation?.sensors?.map((item) => ({id: item.id, type: item.type})))}`);
    const endpoint = sensor.video.url.replace(/^http:\/\/localhost:8889/, media);

    server = http.createServer((_, response) => {
      response.writeHead(200, {'content-type': 'text/html'});
      response.end('<!doctype html><html><body><video id="camera" autoplay muted playsinline></video></body></html>');
    });
    await new Promise((resolve) => server.listen(3000, '0.0.0.0', resolve));
    browser = await chromium.launch({channel: 'chrome', headless: true, args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required']});
    const page = await browser.newPage();
    await page.goto('http://localhost:3000');
    const result = await page.evaluate(async (whepUrl) => {
      async function openReader() {
        const pc = new RTCPeerConnection({iceServers: []});
        const video = document.createElement('video');
        video.autoplay = true; video.muted = true; video.playsInline = true;
        document.body.appendChild(video);
        pc.addTransceiver('video', {direction: 'recvonly'});
        pc.ontrack = (event) => {
          video.srcObject = event.streams[0] || new MediaStream([event.track]);
          video.play().catch(() => {});
        };
        await pc.setLocalDescription(await pc.createOffer());
        await new Promise((resolve, reject) => {
          if (pc.iceGatheringState === 'complete') return resolve();
          const timeout = setTimeout(() => reject(new Error('ICE gathering timeout')), 10000);
          pc.onicegatheringstatechange = () => {
            if (pc.iceGatheringState === 'complete') { clearTimeout(timeout); resolve(); }
          };
        });
        const response = await fetch(whepUrl, {method: 'POST', headers: {'content-type': 'application/sdp'}, body: pc.localDescription.sdp});
        if (!response.ok) throw new Error(`WHEP offer failed ${response.status}: ${await response.text()}`);
        await pc.setRemoteDescription({type: 'answer', sdp: await response.text()});
        await new Promise((resolve, reject) => {
          const timeout = setTimeout(async () => {
            const receivers = pc.getReceivers().map((receiver) => ({kind: receiver.track.kind, state: receiver.track.readyState, muted: receiver.track.muted}));
            const stats = [...(await pc.getStats()).values()].filter((report) => report.type === 'inbound-rtp');
            reject(new Error(`No decoded frame; connection=${pc.connectionState}, receivers=${JSON.stringify(receivers)}, stats=${JSON.stringify(stats)}`));
          }, 25000);
          video.requestVideoFrameCallback(() => { clearTimeout(timeout); resolve(); });
        });
        return {pc, video};
      }
      const first = await openReader();
      const second = await openReader();
      first.pc.close(); first.video.remove();
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error('Stream stopped when the first of two readers left')), 10000);
        second.video.requestVideoFrameCallback(() => { clearTimeout(timeout); resolve(); });
      });
      const result = {width: second.video.videoWidth, height: second.video.videoHeight, readers: 2};
      second.pc.close(); second.video.remove();
      return result;
    }, endpoint);
    if (result.width <= 0 || result.height <= 0) throw new Error(`Invalid decoded frame: ${JSON.stringify(result)}`);
    await new Promise((resolve) => setTimeout(resolve, 7000));
    const finalSensor = await fetch(`${backend}/api/v1/drones/${droneId}/sensors/${sensor.id}/`);
    const sensorState = await finalSensor.json();
    if (sensorState.video.active !== false) throw new Error(`MediaMTX last-reader cleanup did not deactivate camera: ${JSON.stringify(sensorState.video)}`);
    console.log(`PASS browser WebRTC decoded frame ${result.width}x${result.height}; two readers shared publication and last-reader cleanup completed for ${droneId}`);
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise((resolve) => server.close(resolve));
    if (droneId) {
      await fetch(`${backend}/api/v1/drones/${droneId}/`, {method: 'DELETE'});
      await new Promise((resolve) => setTimeout(resolve, 7000));
    }
  }
}

main().catch((error) => { console.error(error); process.exit(1); });
