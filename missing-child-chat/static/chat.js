// 실종아동 발견 신고 채팅 클라이언트.
// 순수 WebSocket으로 서버와 통신하고, 끊기면 최대 3회 자동 재연결한다.

(function () {
  "use strict";

  const config = window.CHAT_CONFIG || {};
  const messagesEl = document.getElementById("messages");
  const formEl = document.getElementById("chatForm");
  const inputEl = document.getElementById("msgInput");
  const statusEl = document.getElementById("connStatus");

  const MAX_RECONNECT = 3; // 재연결 최대 시도 횟수
  let reconnectCount = 0;
  let ws = null;
  let manualClose = false;

  // ws_url이 절대경로가 아니면 현재 호스트 기준 절대 URL로 변환한다.
  function resolveWsUrl() {
    const raw = config.wsUrl || "/ws/chat/" + config.roomId + "?role=" + config.role;
    if (raw.startsWith("ws://") || raw.startsWith("wss://")) {
      return raw;
    }
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + window.location.host + raw;
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "conn-status " + cls;
  }

  // HTML 이스케이프: 사용자 입력을 그대로 DOM에 넣지 않도록 방어(XSS 방지).
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // 시스템 안내 메시지(가운데 정렬).
  function renderSystem(content) {
    const el = document.createElement("div");
    el.className = "msg-system";
    el.textContent = content;
    messagesEl.appendChild(el);
    scrollToBottom();
  }

  // 일반 텍스트 말풍선. 내 메시지는 오른쪽, 상대는 왼쪽.
  function renderText(msg) {
    const mine = msg.sender_role === config.role;
    const row = document.createElement("div");
    row.className = "msg-row " + (mine ? "mine" : "theirs");

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (!mine) {
      const who = document.createElement("div");
      who.className = "sender-label";
      who.textContent = msg.sender_role === "guardian" ? "보호자" : "발견자";
      bubble.appendChild(who);
    }

    const body = document.createElement("div");
    body.className = "bubble-text";
    body.innerHTML = escapeHtml(msg.content);
    bubble.appendChild(body);

    row.appendChild(bubble);
    messagesEl.appendChild(row);
    scrollToBottom();
  }

  // 위치 메시지 말풍선. 지도 링크(구글 지도)를 새 탭으로 열 수 있게 렌더링한다.
  function renderLocation(msg) {
    const mine = msg.sender_role === config.role;
    const row = document.createElement("div");
    row.className = "msg-row " + (mine ? "mine" : "theirs");

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (!mine) {
      const who = document.createElement("div");
      who.className = "sender-label";
      who.textContent = msg.sender_role === "guardian" ? "보호자" : "발견자";
      bubble.appendChild(who);
    }

    const lat = Number(msg.latitude);
    const lng = Number(msg.longitude);
    const mapUrl = "https://www.google.com/maps?q=" + lat + "," + lng;

    const link = document.createElement("a");
    link.className = "location-link";
    link.href = mapUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "📍 공유된 위치 보기 (지도 열기)";
    bubble.appendChild(link);

    const coords = document.createElement("div");
    coords.className = "location-coords";
    coords.textContent = lat.toFixed(5) + ", " + lng.toFixed(5);
    bubble.appendChild(coords);

    row.appendChild(bubble);
    messagesEl.appendChild(row);
    scrollToBottom();
  }

  function renderMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "system") {
      renderSystem(msg.content);
    } else if (msg.type === "text") {
      renderText(msg);
    } else if (msg.type === "location") {
      renderLocation(msg);
    }
  }

  function handleServerMessage(data) {
    if (data.type === "history") {
      // 재접속 시 이전 대화 이력을 순서대로 렌더링.
      (data.messages || []).forEach(renderMessage);
      return;
    }
    renderMessage(data);
  }

  function connect() {
    setStatus("연결 중…", "conn-connecting");
    try {
      ws = new WebSocket(resolveWsUrl());
    } catch (err) {
      setStatus("연결 실패", "conn-error");
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      reconnectCount = 0;
      setStatus("연결됨", "conn-ok");
      inputEl.disabled = false;
    };

    ws.onmessage = function (event) {
      try {
        const data = JSON.parse(event.data);
        handleServerMessage(data);
      } catch (err) {
        // 서버가 보낸 데이터가 JSON이 아니면 무시(정상 상황에선 발생하지 않음).
        console.error("메시지 파싱 실패:", err);
      }
    };

    ws.onclose = function () {
      inputEl.disabled = true;
      if (manualClose) return;
      scheduleReconnect();
    };

    ws.onerror = function () {
      // onerror 직후 onclose가 이어지므로 여기서는 상태 표시만 갱신.
      setStatus("연결 오류", "conn-error");
    };
  }

  function scheduleReconnect() {
    if (reconnectCount >= MAX_RECONNECT) {
      setStatus("연결이 끊겼습니다. 새로고침 해주세요", "conn-error");
      renderSystem("연결이 끊겼습니다. 새로고침 해주세요.");
      return;
    }
    reconnectCount += 1;
    setStatus("재연결 시도 " + reconnectCount + "/" + MAX_RECONNECT + "…", "conn-connecting");
    // 점진적 지연(1s, 2s, 3s) 후 재시도.
    setTimeout(connect, reconnectCount * 1000);
  }

  formEl.addEventListener("submit", function (e) {
    e.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      renderSystem("아직 연결되지 않았습니다. 잠시만 기다려주세요.");
      return;
    }
    ws.send(JSON.stringify({ type: "text", content: text }));
    inputEl.value = "";
    inputEl.focus();
  });

  // ---------- 위치 공유(발견자 전용) ----------
  const locationBar = document.getElementById("locationBar");
  const shareLocationBtn = document.getElementById("shareLocationBtn");

  // 발견자에게만 위치 공유 버튼을 노출한다.
  if (config.role === "finder" && locationBar) {
    locationBar.hidden = false;
  }

  if (shareLocationBtn) {
    shareLocationBtn.addEventListener("click", function () {
      if (!("geolocation" in navigator)) {
        renderSystem("이 브라우저는 위치 기능을 지원하지 않습니다.");
        return;
      }
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        renderSystem("연결된 후에 위치를 공유할 수 있습니다.");
        return;
      }

      shareLocationBtn.disabled = true;
      shareLocationBtn.textContent = "📍 위치 확인 중…";

      navigator.geolocation.getCurrentPosition(
        function (position) {
          ws.send(
            JSON.stringify({
              type: "location",
              latitude: position.coords.latitude,
              longitude: position.coords.longitude,
            })
          );
          shareLocationBtn.disabled = false;
          shareLocationBtn.textContent = "📍 내 위치 공유하기";
        },
        function (error) {
          // 권한 거부/시간 초과 등. 사용자에게 원인을 안내한다.
          let reason = "위치를 가져오지 못했습니다.";
          if (error.code === error.PERMISSION_DENIED) {
            reason = "위치 권한이 거부되었습니다. 브라우저 설정에서 허용해주세요.";
          } else if (error.code === error.TIMEOUT) {
            reason = "위치 확인 시간이 초과되었습니다. 다시 시도해주세요.";
          }
          renderSystem(reason);
          shareLocationBtn.disabled = false;
          shareLocationBtn.textContent = "📍 내 위치 공유하기";
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
      );
    });
  }

  // 페이지를 떠날 때는 재연결하지 않도록 표시.
  window.addEventListener("beforeunload", function () {
    manualClose = true;
    if (ws) ws.close();
  });

  connect();
})();
