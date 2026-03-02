/**
 * n8n Code Node: Aggregate Data
 * Агрегация данных TGStat для GPT-анализа.
 * Узел получает данные из TGStat: Channel Info, Channel Stats, Posts.
 * На выходе — единый JSON для отправки в GPT.
 */

const vars = $('Set Variables').first().json;
const channelInfo = $('TGStat: Channel Info').first().json;
const channelStats = $('TGStat: Channel Stats').first().json;
const postsData = $('TGStat: Posts').first().json;

const info = channelInfo.response || channelInfo;
const stats = channelStats.response || channelStats;
const posts = (postsData.response?.items || postsData.items || []);

const postsCount = posts.length;
const totalViews = posts.reduce((sum, p) => sum + (p.views || 0), 0);
const avgViews = postsCount > 0 ? Math.round(totalViews / postsCount) : 0;

const topPosts = [...posts]
  .sort((a, b) => (b.views || 0) - (a.views || 0))
  .slice(0, 5)
  .map(p => ({
    link: p.link || '',
    views: p.views || 0,
    date: p.date ? new Date(p.date * 1000).toISOString().split('T')[0] : 'unknown',
    text_preview: (p.text || '').substring(0, 120),
    forwards: p.forwards_count || p.forwards || 0,
    reactions: p.reactions_count || 0
  }));

let trend = 'stable';
if (posts.length >= 4) {
  const mid = Math.floor(posts.length / 2);
  const sorted = [...posts].sort((a, b) => (a.date || 0) - (b.date || 0));
  const firstHalf = sorted.slice(0, mid);
  const secondHalf = sorted.slice(mid);
  const avgFirst = firstHalf.reduce((s, p) => s + (p.views || 0), 0) / firstHalf.length;
  const avgSecond = secondHalf.reduce((s, p) => s + (p.views || 0), 0) / secondHalf.length;
  if (avgSecond > avgFirst * 1.1) trend = 'up';
  else if (avgSecond < avgFirst * 0.9) trend = 'down';
}

const startDate = new Date(vars.period_start * 1000).toISOString().split('T')[0];
const endDate = new Date(vars.period_end * 1000).toISOString().split('T')[0];

const aggregated = {
  period: `${startDate} — ${endDate}`,
  channel: {
    title: info.title || info.channel_name || 'MOST',
    username: info.username || vars.channel_id,
    participants_count: stats.participants_count || info.participants_count || 0,
    avg_post_reach: stats.avg_post_reach || avgViews,
    err_percent: stats.err_percent || (stats.participants_count
      ? Math.round(avgViews / stats.participants_count * 100 * 10) / 10
      : 0),
    daily_reach: stats.daily_reach || 0,
    ci_index: stats.ci_index || 0
  },
  posts_count: postsCount,
  total_views: totalViews,
  avg_views: avgViews,
  top_posts: topPosts,
  metrics_trend: trend
};

return [{
  json: {
    aggregated_data: aggregated,
    aggregated_json: JSON.stringify(aggregated, null, 2)
  }
}];
