/**
 * Drive a real drag against the live map, with events the browser considers trusted.
 *
 * Synthetic `MouseEvent`s do not answer the drag question. Measured: dispatching them reaches a
 * `Polyline`'s own `mousedown` listener but reaches **no** Map-level listener at all — not even
 * `mousedown`, which certainly exists. That control is what turns "the API did not deliver
 * mousemove" from a finding into a fact about the harness, and it is why this file exists:
 * `Input.dispatchMouseEvent` over the DevTools protocol produces events with `isTrusted` set,
 * which is the only kind the Maps interaction layer acts on.
 *
 *     node trusted-input.mjs <debug-port> <x> <y>
 *
 * Depends on `ws`, which is present as a transitive dependency rather than a declared one. That
 * is tolerable for a tool nobody's build runs, and it fails loudly rather than silently if it
 * ever goes away.
 */
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
let WebSocket
try {
  WebSocket = require('ws')
} catch {
  console.error('needs `ws`; it was a transitive dependency and appears to be gone')
  process.exit(2)
}

const [port, startX, startY, gesture = 'drag'] = process.argv.slice(2)
if (port === undefined || startX === undefined || startY === undefined) {
  console.error('usage: node trusted-input.mjs <debug-port> <x> <y> [drag|click]')
  process.exit(2)
}

const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = targets.find((each) => each.type === 'page' && each.webSocketDebuggerUrl)
if (page === undefined) {
  console.error('no page target on that port')
  process.exit(1)
}

const socket = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  socket.once('open', resolve)
  socket.once('error', reject)
})

let sequence = 0
const send = (method, params) =>
  new Promise((resolve) => {
    const id = ++sequence
    const onMessage = (raw) => {
      const message = JSON.parse(String(raw))
      if (message.id !== id) return
      socket.off('message', onMessage)
      resolve(message.result)
    }
    socket.on('message', onMessage)
    socket.send(JSON.stringify({ id, method, params }))
  })

const mouse = (type, x, y, extra = {}) =>
  send('Input.dispatchMouseEvent', {
    type,
    x,
    y,
    button: 'left',
    buttons: type === 'mouseReleased' ? 0 : 1,
    clickCount: 1,
    ...extra,
  })

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const x = Number(startX)
const y = Number(startY)

if (gesture === 'click') {
  // A press and a release in the same place. The control for "no click followed the drag":
  // a click suppressed because the pointer travelled is the API, and no click at all is this
  // file failing to produce one.
  await mouse('mousePressed', x, y)
  await wait(60)
  await mouse('mouseReleased', x, y)
  await wait(400)
} else {
  // A press on the line, three moves away from it, a release. Three moves rather than one
  // because a drag is a stream and a single jump is not — the throttle and the handle both key
  // off movement continuing.
  await mouse('mousePressed', x, y)
  await wait(120)
  for (const step of [1, 2, 3]) {
    await mouse('mouseMoved', x + step * 18, y + step * 12)
    await wait(90)
  }
  await mouse('mouseReleased', x + 54, y + 36)
  await wait(400)
}

// Let the page carry on: it is parked waiting for this, so that its listeners are recorded
// after the gesture rather than before it.
await send('Runtime.evaluate', { expression: 'window.__gestureDone?.()' })
socket.close()
console.log('gesture delivered')
