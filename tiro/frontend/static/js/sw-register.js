// Tiro service worker registration (M3.1 Task 2).
//
// Single tiny module rather than inlining this twice: it's imported by
// sidebar.js (base.html — every authenticated page) AND directly by
// login.html (which is standalone and does NOT load sidebar.js at all —
// see login.html's own header comment). Registration must happen on
// /login too: a phone can land there with no session yet, and both
// installability and offline support should start from the very first
// page it sees, not only after a successful sign-in.
//
// Feature-detected and silent by design (per the binding spec: "no
// update-nagging UI"): logs to console.debug only, never surfaces a toast
// or banner. Calling `.register()` more than once with the same script URL
// is a no-op per spec (the browser resolves to the existing registration),
// so the "once" requirement doesn't need any extra dedup state here beyond
// each page calling this exactly once on load.
//
// `{ type: "module" }` is an evergreen-browser bet (see sw.js's own header
// comment for why it's worth taking): on an engine old enough to not
// support module service workers, `.register()` rejects and lands in the
// `.catch()` below like any other registration failure -- a graceful
// no-op, not a crash. The app works fully without a service worker; this
// call degrading to nothing is an acceptable failure mode, not a bug.
export function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker
        .register("/sw.js", { type: "module" })
        .then((registration) => {
            console.debug("Tiro: service worker registered", registration.scope);
        })
        .catch((err) => {
            console.debug("Tiro: service worker registration failed", err);
        });
}
