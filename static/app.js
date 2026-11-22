// Чистый JS-перевод src/App.jsx: без React, без сборщика, без npm.
(function () {
  'use strict';

  const state = {
    messages: [],
    input: '',
  };

  const root = document.getElementById('root');

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === undefined || value === null || value === false) continue;
        if (key === 'className') node.className = value;
        else if (key.startsWith('on') && typeof value === 'function') {
          node.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (key === 'disabled') node.disabled = !!value;
        else node.setAttribute(key, value);
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  // Превращает ответ движка run в содержимое сообщения ассистента.
  // Возвращает { text, options }: options — список карточек товаров (или null).
  function formatEngineReply(data) {
    if (!data || typeof data !== 'object') {
      return { text: 'Не удалось получить ответ.', options: null };
    }
    if (data.status === 'clarify' && Array.isArray(data.questions)) {
      return { text: data.questions.map((q) => q.text).join(' '), options: null };
    }
    if (data.status === 'ok' && Array.isArray(data.options)) {
      if (data.options.length === 0) {
        return { text: 'Подходящих вариантов не нашлось.', options: null };
      }
      return { text: 'Вот что удалось подобрать:', options: data.options };
    }
    return {
      text: data.message || 'Не удалось подобрать варианты. Попробуйте уточнить запрос.',
      options: null,
    };
  }

  // Карточка одного варианта: цена, ID товаров и объяснение.
  function renderOptionCard(opt, index) {
    const ids = Array.isArray(opt.listing_ids) ? opt.listing_ids : [];
    return el(
      'div',
      { className: 'option-card' },
      el(
        'div',
        { className: 'option-card-header' },
        el('span', { className: 'option-card-index' }, `Вариант ${index + 1}`),
        el('span', { className: 'option-card-price' }, `${Number(opt.total_rub).toLocaleString('ru-RU')} ₽`)
      ),
      el(
        'div',
        { className: 'option-card-ids' },
        el('span', { className: 'option-card-label' }, 'ID товаров:'),
        ids.map((id) => el('code', { className: 'option-card-id' }, String(id)))
      ),
      el('p', { className: 'option-card-explanation' }, opt.explanation || '')
    );
  }

  async function sendMessage(e) {
    e.preventDefault();

    const text = state.input.trim();
    if (!text || state.pending) return;

    state.messages = [...state.messages, { text, sender: 'user' }];
    state.input = '';
    state.pending = true;
    render();

    // История для движка: роли user/assistant, последние сообщения.
    const history = state.messages.map((m) => ({
      role: m.sender,
      text: m.text,
    }));

    let reply;
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: history }),
      });
      reply = formatEngineReply(await res.json());
    } catch (err) {
      reply = { text: 'Сервер недоступен. Проверьте, что запущен web_server.py.', options: null };
    }

    state.messages = [
      ...state.messages,
      { text: reply.text, options: reply.options, sender: 'assistant' },
    ];
    state.pending = false;
    render();
    document.querySelector('textarea')?.focus();
  }

  function renderChat() {
    const welcome = el(
      'div',
      { className: 'welcome' },
      el('div', { className: 'welcome-orb' }, el('span', null, '✧')),
      el('span', { className: 'welcome-label' }, 'УМНЫЙ ПОДБОР ТОВАРОВ'),
      el(
        'h1',
        null,
        'Найдём то,',
        el('br'),
        el('span', null, 'что подходит именно вам.')
      ),
      el(
        'p',
        null,
        'Расскажите, какой товар вы ищете, что для вас важно и на какой бюджет рассчитываете. Я помогу найти подходящие варианты.'
      )
    );

    const list = el(
      'div',
      { className: 'messages-list' },
      state.messages.map((message) =>
        el(
          'div',
          { className: `message-row ${message.sender}` },
          message.sender === 'assistant' ? el('div', { className: 'message-avatar' }, '✳') : null,
          el(
            'div',
            { className: `message-bubble ${message.sender}` },
            message.text,
            message.options
              ? el(
                  'div',
                  { className: 'option-cards' },
                  message.options.map((opt, i) => renderOptionCard(opt, i))
                )
              : null
          )
        )
      )
    );

    return el(
      'div',
      { className: 'chat-content' },
      state.messages.length === 0 ? welcome : list
    );
  }

  function renderInput() {
    const textarea = el('textarea', {
      placeholder: 'Какой товар вы хотите подобрать?',
      rows: '1',
    });
    textarea.value = state.input;
    textarea.addEventListener('input', (e) => {
      state.input = e.target.value;
      sendButton.disabled = !state.input.trim();
    });

    const sendButton = el(
      'button',
      { type: 'submit', className: 'send-button', disabled: !state.input.trim() },
      '↑'
    );

    const form = el(
      'form',
      { className: 'message-input', onsubmit: sendMessage },
      textarea,
      el(
        'div',
        { className: 'input-actions' },
        el(
          'button',
          {
            type: 'button',
            className: 'attach-button',
            title: 'Вложения появятся позже',
          },
          '＋'
        ),
        sendButton
      )
    );

    return el(
      'div',
      { className: 'input-wrapper' },
      form,
      el(
        'p',
        { className: 'input-note' },
        'Рекомендации AI могут содержать неточности. Проверяйте характеристики товаров перед покупкой.'
      )
    );
  }

  function render() {
    const app = el(
      'main',
      { className: 'app' },
      el(
        'section',
        { className: 'chat-area' },
        el(
          'header',
          { className: 'chat-header' },
          el(
            'div',
            { className: 'assistant-status' },
            el('div', { className: 'assistant-icon' }, '✳'),
            el(
              'div',
              null,
              el('strong', null, 'Авито AI'),
              el('span', null, 'Персональный подбор товаров')
            )
          )
        ),
        renderChat(),
        renderInput()
      )
    );
    root.replaceChildren(app);
  }

  render();
})();
