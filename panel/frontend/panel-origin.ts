// Where the right-bar panel's backend lives.
//
// panel/backend/server.py serves the panel's data on the same host as the UI, so
// this is derived from the page instead of hardcoded. A hardcoded 127.0.0.1 meant
// a phone — or any browser not running on sn1p — called *its own* loopback, and
// the fetch died with "NetworkError when attempting to fetch resource".
//
// The scheme follows the page on purpose: an HTTPS page fetching an HTTP origin
// is blocked as mixed content, so an HTTPS UI needs an HTTPS panel origin.
// `tailscale serve --tls-terminated-tcp 9010 tcp://127.0.0.1:9010` publishes
// exactly that next to sn1p's HTTPS UI on the tailnet.
//
// Ports: 9010 (PANEL_PORT in server.py).
const PANEL_PORT = 9010;

function derivePanelOrigin(): string {
	// location is absent during SSR/prerender of the static build.
	if (typeof location !== 'undefined' && location.hostname) {
		return `${location.protocol}//${location.hostname}:${PANEL_PORT}`;
	}
	return `http://127.0.0.1:${PANEL_PORT}`;
}

export const PANEL_ORIGIN: string = derivePanelOrigin();

/** Absolute URL for a panel backend path, e.g. panelUrl('/api/dashboard'). */
export function panelUrl(path: string): string {
	return `${PANEL_ORIGIN}${path}`;
}
