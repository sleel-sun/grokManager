window.renderSiteFooter = async function renderSiteFooter() {
  const footer = document.querySelector('.site-footer');
  if (footer) footer.remove();
};

const _bootSiteFooter = () => {
  if (typeof window.renderSiteFooter === 'function') {
    void window.renderSiteFooter();
  }
};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _bootSiteFooter, { once: true });
} else {
  _bootSiteFooter();
}

window.addEventListener('pageshow', _bootSiteFooter);
