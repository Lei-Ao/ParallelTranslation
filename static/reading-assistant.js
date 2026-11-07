'use strict';
(() => {
  const source = $('#source-pane'), tools = $('#excerpt-tools'), panel = $('#reading-assistant');
  const question = $('#excerpt-question'), form = $('#reading-question'), answers = $('#reading-answers');
  const excerpts = [];
  let active = null, frame = 0, session = 0, busy = false;

  function hideTools() {
    active = null;
    tools.classList.add('hidden');
    document.body.classList.remove('selecting-source');
  }

  function positionTools() {
    const selection = window.getSelection();
    if (!selection.rangeCount || selection.isCollapsed) return hideTools();
    const range = selection.getRangeAt(0);
    const layer = range.startContainer.parentElement?.closest('.pdf-text-layer');
    if (!layer || !source.contains(layer) || !layer.contains(range.endContainer)) return hideTools();
    const glyph = range.startContainer.parentElement.closest('.pdf-char');
    const box = glyph?.getBoundingClientRect() || range.getBoundingClientRect();
    const pane = source.getBoundingClientRect();
    if (box.bottom < pane.top || box.top > pane.bottom || box.right < pane.left || box.left > pane.right) return hideTools();
    active = {range:range.cloneRange(), page:state.doc.pages[Number(layer.closest('[data-page-index]').dataset.pageIndex)].number};
    tools.classList.remove('hidden');
    document.body.classList.add('selecting-source');
    const width = tools.offsetWidth, height = tools.offsetHeight;
    tools.style.left = Math.max(4, Math.min(box.left, window.innerWidth - width - 4)) + 'px';
    tools.style.top = (box.top - height - 6 >= pane.top ? box.top - height - 6 : Math.min(window.innerHeight - height, box.bottom + 6)) + 'px';
  }

  function scheduleTools() {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(positionTools);
  }
  document.addEventListener('selectionchange', scheduleTools);
  source.addEventListener('scroll', scheduleTools, {passive:true});
  window.addEventListener('resize', scheduleTools);

  function renderExcerpts() {
    $('#reading-excerpts').replaceChildren(...excerpts.map((excerpt, index) => {
      const item = document.createElement('blockquote'), remove = document.createElement('button');
      item.textContent = excerpt;
      remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', '移除引文');
      remove.onclick = () => { excerpts.splice(index, 1); renderExcerpts(); };
      item.append(remove);
      return item;
    }));
    form.querySelector('button.primary').disabled = busy || !excerpts.length;
  }
  tools.onmousedown = event => event.preventDefault();
  tools.onclick = event => {
    const action = event.target.closest('[data-tool]')?.dataset.tool;
    if (!active || !action) return;
    const {range, page} = active;
    if (action === 'highlight') {
      const highlights = CSS.highlights;
      if (!highlights) return toast('当前浏览器不支持文字高亮');
      if (!highlights.has('reader-marks')) highlights.set('reader-marks', new Highlight());
      highlights.get('reader-marks').add(range);
    } else {
      const fragment = range.cloneContents();
      fragment.querySelectorAll('br').forEach(br => br.replaceWith('\n'));
      const text = fragment.textContent.trim();
      if (!text) return;
      const excerpt = `第 ${page} 页\n${text}`;
      if (action !== 'quote') {
        if (busy) return toast('请等待当前回答完成');
        if (excerpt.length > 18000) return toast('引文过长，请缩短选区');
        panel.classList.remove('hidden');
        ask(action === 'translate' ? '请翻译引用的原文。' : '请解释引用的原文。', excerpt);
      } else {
        if (!excerpts.includes(excerpt)) {
          if ([...excerpts, excerpt].join('\n\n').length > 18000) return toast('引文过长，请缩短选区或移除已有引文');
          excerpts.push(excerpt);
        }
        panel.classList.remove('hidden');
        renderExcerpts();
        question.focus();
      }
    }
    window.getSelection().removeAllRanges();
    hideTools();
  };
  form.onsubmit = event => {
    event.preventDefault();
    ask(question.value.trim(), excerpts.join('\n\n'), true);
  };
  async function ask(prompt, quotes, fromComposer = false) {
    const current = session;
    if (busy || !prompt || !quotes) return;
    busy = true; renderExcerpts(); question.readOnly = true;
    const turn = document.createElement('article'), heading = document.createElement('strong');
    const citation = document.createElement('blockquote'), reply = document.createElement('p');
    heading.textContent = prompt; citation.textContent = quotes; reply.textContent = '正在回答…';
    turn.append(citation, heading, reply); answers.append(turn); answers.scrollTop = answers.scrollHeight;
    try {
      const result = await api('/api/reader/ask', {method:'POST', body:JSON.stringify({
        agent:state.agent, target:state.target, excerpts:quotes, question:prompt
      })});
      if (current !== session) return;
      reply.textContent = result.answer;
      if (fromComposer) question.value = '';
    } catch (error) {
      reply.textContent = error.message;
    } finally {
      if (current === session) { busy = false; question.readOnly = false; renderExcerpts(); }
    }
  }
  $('#open-assistant').onclick = () => { $('#reader-menu').open = false; panel.classList.remove('hidden'); renderExcerpts(); question.focus(); };
  $('#close-assistant').onclick = () => panel.classList.add('hidden');
  globalThis.resetReadingAssistant = () => {
    session++; busy = false; question.value = ''; question.readOnly = false;
    excerpts.length = 0; answers.replaceChildren(); renderExcerpts(); hideTools();
    panel.classList.add('hidden'); $('#enable-edit').checked = false;
    globalThis.CSS?.highlights?.delete('reader-marks');
  };
})();
