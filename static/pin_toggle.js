// PIN/비밀번호 입력란의 보기·숨기기 눈 아이콘 토글. login/register/reset_pin 공용.
(function () {
  "use strict";

  document.querySelectorAll(".pin-toggle-btn").forEach(function (btn) {
    const input = document.getElementById(btn.dataset.target);
    const eyeOn = btn.querySelector(".icon-eye");
    const eyeOff = btn.querySelector(".icon-eye-off");
    if (!input) return;

    btn.addEventListener("click", function () {
      const isPassword = input.type === "password";
      input.type = isPassword ? "text" : "password";
      eyeOn.hidden = isPassword;
      eyeOff.hidden = !isPassword;
      btn.setAttribute("aria-label", isPassword ? "PIN 숨기기" : "PIN 표시");
    });
  });
})();
