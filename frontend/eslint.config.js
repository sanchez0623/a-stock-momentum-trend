// ESLint flat config (ESLint 10)
// 规则基线: js recommended + typescript-eslint recommended + react-hooks
// 关键: react-hooks/exhaustive-deps 可拦截依赖数组漏项(曾导致选股页白屏)
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs['flat/recommended'].rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // 配置树为任意 JSON, 放宽 any(见 Settings.tsx 顶部注释)
      '@typescript-eslint/no-explicit-any': 'off',
      // 类型导入由 tsc noUnusedLocals 把关, 此处不重复拦截
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' }],
    },
  },
  {
    // UI 组件库文件: 组件与常量/工具函数混导是库文件常态, fast refresh 规则豁免
    files: ['src/components/ui/**'],
    rules: { 'react-refresh/only-export-components': 'off' },
  },
)
