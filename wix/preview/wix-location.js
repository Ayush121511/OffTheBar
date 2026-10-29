// Stand-in for Wix's `wix-location` module, used only by the local preview.
const params = new URLSearchParams(window.location.search);

export default {
  query: Object.fromEntries(params),
  to() {
    // Like Wix navigating to /offthebar: drop ?msg=, but keep the chosen backend.
    const backend = params.get('backend');
    window.location.href = window.location.pathname + (backend ? `?backend=${backend}` : '');
  }
};
