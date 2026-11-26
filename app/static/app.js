// 根据卷数/对数选择框显示对应行，并禁用隐藏行的输入，避免提交多余数据。
(function () {
  function syncRows(selectId, rowClass) {
    var select = document.getElementById(selectId);
    if (!select) return;
    var count = parseInt(select.value, 10);
    var rows = document.querySelectorAll("." + rowClass);
    rows.forEach(function (row) {
      var idx = parseInt(row.getAttribute("data-index"), 10);
      var active = idx < count;
      row.hidden = !active;
      row.querySelectorAll("input, select").forEach(function (el) {
        el.disabled = !active;
      });
    });
  }

  function syncAll() {
    syncRows("roll_count", "roll-row");
    syncRows("pair_count", "pair-row");
  }

  document.addEventListener("DOMContentLoaded", function () {
    syncAll();
    var rc = document.getElementById("roll_count");
    var pc = document.getElementById("pair_count");
    if (rc) rc.addEventListener("change", syncAll);
    if (pc) pc.addEventListener("change", syncAll);
  });
})();
