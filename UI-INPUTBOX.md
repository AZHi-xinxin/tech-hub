# UI 输入框自适应设计（UI-INPUTBOX）

本文档说明 tech-hub 群聊输入框的自适应行为：**宽度恒为整行、高度随文字行数增长**，微信聊天输入框风格。包含行为规格、实现要点、键盘交互与验证清单。

## 1. 需求背景

早期版本输入框是单行 `<input type="text">`，长文本只能横向滚动，编辑体验差。需求迭代过程：

| 版本 | 设计 | 验收结论 |
| --- | --- | --- |
| v1 | 输入框随文字长度拉宽，上限为聊天界面宽度 50% | 被否：空白态缩窄、宽不到满行、手机端不生效 |
| v2 | 输入框随最长一行拉宽到满行，高度随行数增长 | 通过，但空白态仍不是满行宽 |
| v3（定稿） | **宽度恒为满行，只有高度随行数变化** | 通过 ✅ |

最终结论：输入框**初始空白时就是整行最宽**，输入过程中宽度不变，文字超出一行时只增加高度——这是微信式输入框的实际形态，也是用户想要的形态。教训：需求里的「自适应拉宽」真实意图是「满宽 + 高自适应」，直接确认比猜测更快。

## 2. 行为规格

| 场景 | 行为 |
| --- | --- |
| 初始空白 | 输入框占满整行宽度，单行高度 |
| 输入一行以内 | 宽度不变，高度不变 |
| 文字超出单行自动换行 | 宽度不变，高度随行数逐行增长 |
| 高度达到聊天卡片高度的一半 | 停止增长，框内出现垂直滚动条，可上下滚动编辑 |
| 发送消息 | 输入框清空，高度自动缩回单行 |
| 切换窗口大小 | 高度上限随聊天卡片实时重算 |

上限取值：`max(120px, 聊天卡片高度 × 50%)`，CSS 兜底 `max-height:40vh`。高度下限 38px（单行文字 + 内边距 + 边框）。

## 3. 实现说明

### 3.1 CSS（一行）

```css
#inbox{flex:1;box-sizing:border-box;resize:none;min-height:38px;max-height:40vh;overflow-y:auto;line-height:1.5}
```

- `flex:1`：在 `.row`（flex 容器）中占满整行——宽度自适应由此天然成立，无需 JS 测量
- `resize:none`：禁用浏览器默认拖拽缩放手柄
- `box-sizing:border-box`：JS 计算高度时包含内边距与边框，避免滚动条跳动
- `overflow-y:auto`：高度触顶后框内滚动

### 3.2 HTML（一行）

```html
<textarea id="inbox" rows="1" placeholder="发消息…"></textarea>
```

单行 `<input type="text">` 换成 `<textarea>`——多行文本与内部滚动是 textarea 的原生能力，不需要自己实现。

### 3.3 JavaScript（核心逻辑）

```javascript
const inboxEl = document.getElementById('inbox');
function fitInbox(){
  const card = document.getElementById('msgs').parentElement;
  const maxH = Math.max(120, card.clientHeight * 0.5);
  inboxEl.style.height = 'auto';
  inboxEl.style.height = Math.min(Math.max(38, inboxEl.scrollHeight), maxH) + 'px';
}
inboxEl.addEventListener('input', fitInbox);
window.addEventListener('resize', fitInbox);
```

要点：

1. **先 `height:auto` 再读 `scrollHeight`**——这是 textarea 高度自适应的经典手法。先把高度放开，让 `scrollHeight` 反映内容的真实高度，再把它写回 `height`。
2. **上限是聊天卡片高度的一半**，而不是固定像素——窗口缩放、手机横竖屏切换都自动适配。
3. 发送后调用一次 `fitInbox()` 让框缩回单行（`sendMsg` 清空 `.value` 后执行）。

### 3.4 键盘交互

```javascript
document.getElementById('inbox').addEventListener('keydown', e=>{
  if(e.key==='Enter' && !e.shiftKey && !e.isComposing){
    e.preventDefault(); sendMsg();
  }
});
```

| 按键 | 行为 |
| --- | --- |
| Enter | 发送消息（`preventDefault` 阻止换行） |
| Shift+Enter | 插入换行 |
| 中文输入法选词回车 | 不发送——`e.isComposing` 守卫，选词确认的回车只结束组合 |

## 4. 移动端注意

- 手机浏览器与桌面共用同一套 HTML/CSS/JS，无任何平台分支，理论上两端行为一致。
- 若升级后手机端看起来「没生效」，优先排查浏览器缓存：强制刷新页面（HTML 无缓存控制头时可能被缓存）。

## 5. 验证清单

- [ ] 初始空白：输入框满行宽、单行高
- [ ] 输入长文本：宽度不变，高度随行数增长
- [ ] 继续输入：高度到聊天卡片约一半后停止，框内出现竖向滚动条，可滚动编辑
- [ ] 发送：输入框清空且缩回单行
- [ ] Enter 发送、Shift+Enter 换行
- [ ] 中文输入法选词回车不误发
- [ ] 缩放窗口/横竖屏：高度上限随卡片重算
- [ ] 手机端与桌面端行为一致
