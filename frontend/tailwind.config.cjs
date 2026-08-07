/** @type {import('tailwindcss').Config} */
// 全局设计 Token：品牌色与语义色统一在此定义, 组件中一律使用语义类名(如 text-rise/bg-primary)
// 涨跌遵循 A 股惯例: 涨=红(rise) 跌=绿(fall)
module.exports = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // 品牌主色: A股红(原 antd colorPrimary #d32029)
        primary: {
          DEFAULT: '#d32029',
          light: '#ff7875',
          dark: '#a8071a',
        },
        // 导航/链接蓝
        link: '#2563eb',
        // 涨跌语义色
        rise: '#dc2626', // 涨(红)
        fall: '#16a34a', // 跌(绿)
        // 中性文字
        ink: {
          DEFAULT: '#333333',
          secondary: '#666666',
          muted: '#888888',
          faint: '#999999',
        },
        // 边框与分隔
        line: '#e5e6eb',
        divider: '#f0f1f3',
      },
      borderRadius: {
        // 原 antd token borderRadius=6
        DEFAULT: '6px',
      },
      fontSize: {
        // 原 antd token fontSize=13, 作为页面正文基准
        base: '13px',
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,0.04), 0 1px 6px rgba(0,0,0,0.04)',
        cardHover: '0 4px 16px rgba(0,0,0,0.10)',
      },
      fontFamily: {
        sans: ['system-ui', '"Microsoft YaHei"', '-apple-system', '"Segoe UI"', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
