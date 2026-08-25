/**
 * Shell layout: big map on the left, chat rail on the right.
 *
 * Placeholder panes only — the Google Maps canvas and the tool-calling assistant land in
 * later work. The split lives here because every feature is required to be reachable both
 * ways: anything the assistant can do must also be doable with the mouse.
 */
export function App() {
  return (
    <div className="app">
      <main className="map-pane" aria-label="Route map">
        <p className="placeholder">Map canvas</p>
      </main>
      <aside className="chat-pane" aria-label="Trip assistant">
        <p className="greeting">
          Describe your trip and I&rsquo;ll help plan it for you! Or set a start and end point on
          the map.
        </p>
      </aside>
    </div>
  )
}
