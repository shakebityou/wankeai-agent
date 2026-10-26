import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import { router } from './router'
import { installBffErrorHandler } from './api/interceptor-bridge'
import './styles/index.scss'

/**
 * 玩可爱智能客服 Agent —— 前端入口。
 *
 * 全局约定（任务书 4.1 / 4.5）：
 * - 只调 BFF（/bff/v1/*），所有请求经 src/api 统一封装；
 * - 6 个 BFF 错误码由 Axios 拦截器统一处理，页面不重复写错误分支；
 * - 无任何外链资源（HC-1），Element Plus 与 ECharts 全部本地打包。
 */
const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn, size: 'default', zIndex: 3000 })

installBffErrorHandler()

app.mount('#app')
