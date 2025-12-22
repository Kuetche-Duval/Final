// service-worker.js

self.addEventListener("push", function (event) {
  try {
    let data = {};
    try {
      data = event.data.json();
    } catch (e) {
      data.title = "Notification";
      data.body = event.data.text() || "";
    }

    const title = data.title || "Notification";
    const options = {
      body: data.body || "",
      icon: data.icon || "/static/img/logo.png",
      badge: data.badge || "/static/img/logo.png",
      data: {
        url: data.url || "/"
      },
      requireInteraction: data.requireInteraction || false
    };

    event.waitUntil(
      self.registration.showNotification(title, options)
    );
  } catch (e) {
    console.error("Service Worker push event failed:", e);
  }
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();

  const clickResponsePromise = (async () => {
    const urlToOpen = event.notification.data && event.notification.data.url;
    if (urlToOpen) {
      const allClients = await clients.matchAll({
        type: "window",
        includeUncontrolled: true
      });

      let matchingClient = null;
      for (const client of allClients) {
        if (client.url === urlToOpen) {
          matchingClient = client;
          break;
        }
      }
      if (matchingClient) {
        matchingClient.focus();
      } else {
        await clients.openWindow(urlToOpen);
      }
    }
  })();

  event.waitUntil(clickResponsePromise);
});