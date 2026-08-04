// [버그 수정] 발견자가 신고를 시작해도, 이미 열려 있는 보호자 대시보드에는
// "채팅 열기" 버튼이 새로고침 전까지 안 보이던 문제(박선우 리포트) 대응.
// GET /guardian/dashboard/active-rooms를 몇 초 간격으로 폴링해서, 실종 신고
// 중인 아이 카드에 진행 중인 채팅방이 새로 생기면 버튼을 그 자리에서 넣어준다.

(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;

  function buildChatButton(childName, roomId) {
    const a = document.createElement("a");
    a.className = "button active-chat";
    a.href = "/chat/" + roomId + "?role=guardian";
    a.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/><path d="M7 8h10M7 12h6"/></svg>' +
      escapeHtml(childName) +
      " 님을 발견했습니다 — 채팅 열기";
    return a;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function syncCard(card, roomId) {
    const existing = card.querySelector(".active-chat");
    if (roomId) {
      if (existing) {
        // 이미 버튼이 있는데 방 ID가 바뀐 경우(드묾)만 링크를 갱신한다.
        const expectedHref = "/chat/" + roomId + "?role=guardian";
        if (existing.getAttribute("href") !== expectedHref) {
          existing.setAttribute("href", expectedHref);
        }
        return;
      }
      // 아직 버튼이 없으면 새로 만들어서, report-row 바로 다음 위치에 끼워 넣는다
      // (guardian_dashboard.html의 정적 마크업과 동일한 위치).
      const reportRow = card.querySelector(".report-row");
      const button = buildChatButton(card.dataset.childName, roomId);
      if (reportRow && reportRow.nextSibling) {
        reportRow.parentNode.insertBefore(button, reportRow.nextSibling);
      } else if (reportRow) {
        reportRow.parentNode.appendChild(button);
      }
    } else if (existing) {
      // 채팅방이 종료되는 등으로 더 이상 활성 상태가 아니면 버튼을 치운다.
      existing.remove();
    }
  }

  async function poll() {
    try {
      const response = await fetch("/guardian/dashboard/active-rooms");
      if (!response.ok) return; // 세션 만료 등 - 조용히 넘어가고 다음 폴링에서 재시도.
      const data = await response.json();
      const activeRooms = data.active_rooms || {};
      document.querySelectorAll(".child-card[data-child-status='missing']").forEach(function (card) {
        const childId = card.dataset.childId;
        syncCard(card, activeRooms[childId] || null);
      });
    } catch (err) {
      // 네트워크 오류는 조용히 무시하고 다음 폴링에서 재시도한다(부가 기능이라
      // 사용자에게 에러를 노출할 필요가 없다).
    }
  }

  if (document.querySelector(".child-card")) {
    setInterval(poll, POLL_INTERVAL_MS);
  }
})();
