(function () {
  var root = document.documentElement;
  var systemLight = window.matchMedia('(prefers-color-scheme: light)');
  var currentTheme = 'auto';
  var ready = false;
  var logo = null;
  var faviconCache = {};

  function validTheme(value) {
    return value === 'light' || value === 'dark' ? value : 'auto';
  }

  function savedTheme() {
    try {
      return validTheme(localStorage.getItem('theme'));
    } catch (_) {
      return 'auto';
    }
  }

  function resolvedTheme() {
    return currentTheme === 'auto' ? (systemLight.matches ? 'light' : 'dark') : currentTheme;
  }

  function updateFavicon() {
    if (!ready || !logo || !logo.complete || !logo.naturalWidth) return;
    var favicon = document.querySelector('[data-theme-favicon]');
    if (!favicon) return;
    var style = window.getComputedStyle(root);
    var brightness = parseFloat(style.getPropertyValue('--brand-logo-brightness')) || 1;
    var saturation = parseFloat(style.getPropertyValue('--brand-logo-saturation')) || 1;
    var key = resolvedTheme() + ':' + brightness + ':' + saturation;
    try {
      if (!faviconCache[key]) {
        var canvas = document.createElement('canvas');
        canvas.width = canvas.height = 64;
        var context = canvas.getContext('2d');
        if (!context) return;
        context.drawImage(logo, 0, 0, 64, 64);
        var image = context.getImageData(0, 0, 64, 64);
        var pixels = image.data;
        // Match CSS brightness() then saturate(), retaining the original alpha.
        // Pixel conversion also works in browsers without CanvasRenderingContext2D.filter.
        for (var i = 0; i < pixels.length; i += 4) {
          var red = Math.min(255, pixels[i] * brightness);
          var green = Math.min(255, pixels[i + 1] * brightness);
          var blue = Math.min(255, pixels[i + 2] * brightness);
          var luminance = 0.213 * red + 0.715 * green + 0.072 * blue;
          pixels[i] = luminance + (red - luminance) * saturation;
          pixels[i + 1] = luminance + (green - luminance) * saturation;
          pixels[i + 2] = luminance + (blue - luminance) * saturation;
        }
        context.putImageData(image, 0, 0);
        faviconCache[key] = canvas.toDataURL('image/png');
      }
      favicon.type = 'image/png';
      favicon.setAttribute('sizes', '64x64');
      favicon.href = faviconCache[key];
    } catch (_) {
      // Keep the existing ICO if canvas export is unavailable or blocked.
    }
  }

  function updateTheme(theme, persist) {
    currentTheme = validTheme(theme);
    root.setAttribute('data-theme', currentTheme);
    root.setAttribute('data-color-scheme', resolvedTheme());
    if (persist) {
      try {
        localStorage.setItem('theme', currentTheme);
      } catch (_) {
        // Theme controls still work when browser storage is unavailable.
      }
    }
    document.querySelectorAll('[data-theme-btn]').forEach(function (button) {
      var active = button.getAttribute('data-theme-btn') === currentTheme;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    updateFavicon();
  }

  // The inline head bootstrap handles first paint; initialize the live controls here.
  updateTheme(savedTheme(), false);

  function systemChanged() {
    if (currentTheme === 'auto') updateTheme('auto', false);
  }
  if (systemLight.addEventListener) systemLight.addEventListener('change', systemChanged);
  else systemLight.addListener(systemChanged);

  window.addEventListener('storage', function (event) {
    if (event.key === 'theme' || event.key === null) {
      updateTheme(event.newValue, false);
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    ready = true;
    document.querySelectorAll('[data-theme-btn]').forEach(function (button) {
      button.addEventListener('click', function () {
        updateTheme(button.getAttribute('data-theme-btn'), true);
      });
    });
    logo = new Image();
    logo.onload = updateFavicon;
    logo.src = '/static/cal-assistant-logo.png';
    updateTheme(currentTheme, false);
  });
})();
