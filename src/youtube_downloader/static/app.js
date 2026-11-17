// HTMX treats 4xx as errors and refuses to swap them. Our 400/409/429 responses
// are HTML fragments meant for the page, so let them through.
document.addEventListener("htmx:beforeSwap", function (event) {
  var status = event.detail.xhr.status;
  if (status === 400 || status === 404 || status === 409 || status === 429) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});
