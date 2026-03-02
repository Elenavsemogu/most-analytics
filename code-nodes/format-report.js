/**
 * n8n Code Node: Format Report
 * Сборка финального отчёта из агрегированных данных + GPT-анализа.
 * На выходе — текст для отправки в Telegram.
 */

const data = $('Code: Aggregate Data').first().json.aggregated_data;
const gptResponse = $('OpenAI: Analyze').first().json;
const gptText = gptResponse.choices?.[0]?.message?.content || 'GPT-анализ недоступен';

const ch = data.channel;
const period = data.period;

const fmt = (n) => n ? n.toLocaleString('ru-RU') : '0';

const trendIcon = {
  up: '↗️ рост',
  down: '↘️ снижение',
  stable: '➡️ стабильно'
};

let report = `📊 *Еженедельный отчёт MOST*\n`;
report += `📅 ${period}\n\n`;

report += `*Ключевые метрики*\n`;
report += `👥 Подписчики: ${fmt(ch.participants_count)}\n`;
report += `👁 Средний охват поста: ${fmt(ch.avg_post_reach)}\n`;
report += `📈 ERR: ${ch.err_percent}%\n`;
report += `📝 Постов за неделю: ${data.posts_count}\n`;
report += `👀 Всего просмотров: ${fmt(data.total_views)}\n`;
report += `📊 Тренд: ${trendIcon[data.metrics_trend] || '➡️ стабильно'}\n\n`;

report += `---\n\n`;
report += gptText;
report += `\n\n---\n_Отчёт сгенерирован автоматически_`;

return [{
  json: {
    report_text: report,
    report_chat_id: $('Set Variables').first().json.report_chat_id
  }
}];
