/**
 * vue-router 的 RouteMeta 类型增强。
 *
 * 这样 `route.meta.title` 等字段就有类型，不用到处 as 断言。
 */
import 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    /** 菜单/页面标题 */
    title?: string
    /** Element Plus 图标组件名 */
    icon?: string
    /** 所属开发阶段，如「阶段 1」 */
    stage?: string
    /** 该模块是否已开发完成 */
    done?: boolean
    /** 一句话说明，用于占位页与菜单 tooltip */
    description?: string
  }
}
