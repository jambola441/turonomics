/* Receive alerts while the page is closed.

   A service worker is the only way a browser will show a notification for a
   site nobody is looking at, which is the entire point: a street-cleaning
   deadline matters most at 8am when the tab is long gone.

   Kept deliberately small. This file runs outside the page, updates on its own
   schedule, and a bug in it is invisible — so it does two things and no more. */
"use strict";

self.addEventListener("install", function () {
  // Take over immediately rather than waiting for every tab to close. An alert
  // system that needs the operator to quit their browser before a fix lands is
  // not one they will trust.
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    // Not JSON. Showing the raw text beats showing nothing: a push event that
    // ends without showNotification makes Chrome post "This site has been
    // updated in the background" over our name, which looks like a bug to the
    // only person who will ever see it.
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Turonomics", {
      body: data.body || "",
      icon: "icons/icon-192.png",
      badge: "icons/icon-192.png",
      // Keyed on the task and its deadline, so the one-hour warning replaces
      // the twelve-hour one on the lock screen instead of stacking under it.
      tag: data.tag || "turonomics",
      renotify: true,
      // Only a deadline that has passed, or is about to, earns a notification
      // that will not dismiss itself.
      requireInteraction: !!data.urgent,
      data: { url: data.url || "./" }
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "./";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (windows) {
        for (var i = 0; i < windows.length; i += 1) {
          var open = windows[i];
          // Reuse a tab that is already on this site rather than opening a
          // fourth copy of the fleet view every time an alert is tapped.
          if (open.url.indexOf(self.location.origin) === 0 && "focus" in open) {
            if ("navigate" in open) open.navigate(url);
            return open.focus();
          }
        }
        return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
      })
  );
});
