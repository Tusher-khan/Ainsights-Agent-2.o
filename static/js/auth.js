/* AInsight — sign-in, register, reset and profile forms. */
(function () {
  'use strict';

  var CLIENT_HEADER = { 'Content-Type': 'application/json', 'X-AInsight-Client': 'web' };

  function call(method, path, body) {
    return fetch(path, {
      method: method,
      headers: CLIENT_HEADER,
      credentials: 'same-origin',
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.message) || 'Something went wrong. Please try again.');
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }, function () {
      throw new Error('Could not reach the server. Check your connection and try again.');
    });
  }

  function show(form, kind, text) {
    var err = form.querySelector('[data-msg]');
    var ok = form.querySelector('[data-ok]');
    if (err) { err.hidden = true; }
    if (ok) { ok.hidden = true; }
    var target = kind === 'error' ? err : ok;
    if (target && text) { target.textContent = text; target.hidden = false; }
  }

  function busy(form, on) {
    var button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = on; }
  }

  function field(form, name) {
    var el = form.elements[name];
    return el ? el.value : '';
  }

  function nextUrl() {
    var next = new URLSearchParams(location.search).get('next') || '/dashboard';
    // only follow same-site paths, never an absolute or protocol-relative URL
    return /^\/(?!\/)/.test(next) ? next : '/dashboard';
  }

  function token() {
    return new URLSearchParams(location.search).get('token') || '';
  }

  var handlers = {
    login: function (form) {
      return call('POST', '/api/auth/login', { email: field(form, 'email'), password: field(form, 'password') })
        .then(function () { location.href = nextUrl(); });
    },
    register: function (form) {
      return call('POST', '/api/auth/register', {
        name: field(form, 'name'), email: field(form, 'email'), password: field(form, 'password'),
      }).then(function (data) {
        if (data && data.verification_required) {
          show(form, 'ok', 'Account created. Check your email for a confirmation link, then sign in.');
          return;
        }
        location.href = '/dashboard';
      });
    },
    forgot: function (form) {
      return call('POST', '/api/auth/forgot-password', { email: field(form, 'email') })
        .then(function (data) { show(form, 'ok', data.message); });
    },
    reset: function (form) {
      return call('POST', '/api/auth/reset-password', { token: token(), password: field(form, 'password') })
        .then(function (data) {
          show(form, 'ok', data.message);
          setTimeout(function () { location.href = '/login'; }, 1500);
        });
    },
    profile: function (form) {
      return call('PATCH', '/api/auth/me', { name: field(form, 'name') })
        .then(function () { show(form, 'ok', 'Saved.'); });
    },
    'change-password': function (form) {
      return call('POST', '/api/auth/change-password', {
        current_password: field(form, 'current_password'), new_password: field(form, 'new_password'),
      }).then(function () { form.reset(); show(form, 'ok', 'Password changed. Other devices were signed out.'); });
    },
    'delete-account': function (form) {
      if (!window.confirm('Delete your account and all of its data permanently?')) { return Promise.resolve(); }
      return call('DELETE', '/api/auth/account', { password: field(form, 'password') })
        .then(function () { location.href = '/'; });
    },
  };

  document.querySelectorAll('form[data-auth-form]').forEach(function (form) {
    var handler = handlers[form.getAttribute('data-auth-form')];
    if (!handler) { return; }
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      show(form, 'error', '');
      busy(form, true);
      Promise.resolve(handler(form))
        .catch(function (err) { show(form, 'error', err.message); })
        .then(function () { busy(form, false); });
    });
  });

  // Email confirmation page: the link carries the token, so confirm on load.
  var verify = document.querySelector('[data-auth-form="verify"]');
  if (verify) {
    var holder = verify.parentElement;
    var pending = holder.querySelector('[data-pending]');
    call('POST', '/api/auth/verify-email', { token: token() })
      .then(function () {
        pending.hidden = true;
        show(holder, 'ok', 'Your email address is confirmed.');
      })
      .catch(function (err) {
        pending.hidden = true;
        show(holder, 'error', err.message);
      });
  }

  var resend = document.querySelector('[data-resend-verification]');
  if (resend) {
    resend.addEventListener('click', function () {
      call('POST', '/api/auth/resend-verification').then(function (data) {
        resend.textContent = data.message;
        resend.disabled = true;
      }).catch(function (err) { resend.textContent = err.message; });
    });
  }

  // Hide the "create an account" link when sign-ups are closed.
  var registerLink = document.querySelector('[data-register-link]');
  if (registerLink) {
    call('GET', '/api/auth/config').then(function (cfg) {
      if (!cfg.registration_open) { registerLink.hidden = true; }
    }).catch(function () {});
  }
})();
