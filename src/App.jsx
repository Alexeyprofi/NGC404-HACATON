
import { useState } from 'react'
import './App.css'

function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')

  function sendMessage(e) {
    e.preventDefault()

    const text = input.trim()
    if (!text) return

    setMessages((prev) => [
      ...prev,
      {
        id: Date.now(),
        text,
        sender: 'user'
      },
      {
        id: Date.now() + 1,
        text: 'Спасибо за описание! Давайте подберём подходящие варианты. Уточните, пожалуйста, какой бюджет вы готовы выделить на покупку?',
        sender: 'assistant'
      }
    ])

    setInput('')
  }

  return (
    <main className="app">

      <section className="chat-area">

        <header className="chat-header">
          <div className="assistant-status">
            <div className="assistant-icon">✳</div>
            <div>
              <strong>AI Assistant</strong>
              <span>Персональный подбор товаров</span>
            </div>
          </div>
        </header>

        <div className="chat-content">
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="welcome-orb">
                <span>✧</span>
              </div>

              <span className="welcome-label">
                УМНЫЙ ПОДБОР ТОВАРОВ
              </span>

              <h1>
                Найдём то,<br />
                <span>что подходит именно вам.</span>
              </h1>

              <p>
                Расскажите, какой товар вы ищете,
                что для вас важно и на какой бюджет
                рассчитываете. Я помогу найти
                подходящие варианты.
              </p>
            </div>
          ) : (
            <div className="messages-list">
              {messages.map((message) => (
                <div
                  key={message.id}
                  className={`message-row ${message.sender}`}
                >
                  {message.sender === 'assistant' && (
                    <div className="message-avatar">✳</div>
                  )}

                  <div className={`message-bubble ${message.sender}`}>
                    {message.text}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="input-wrapper">
          <form className="message-input" onSubmit={sendMessage}>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Какой товар вы хотите подобрать?"
              rows="1"
            />

            <div className="input-actions">
              <button
                type="button"
                className="attach-button"
                title="Вложения появятся позже"
              >
                ＋
              </button>

              <button
                type="submit"
                className="send-button"
                disabled={!input.trim()}
              >
                ↑
              </button>
            </div>
          </form>

          <p className="input-note">
            Рекомендации AI могут содержать неточности.
            Проверяйте характеристики товаров перед покупкой.
          </p>
        </div>

      </section>
    </main>
  )
}

export default App
