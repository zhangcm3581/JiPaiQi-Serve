const items=[...document.querySelectorAll('.case')];
const search=document.querySelector('#search'), category=document.querySelector('#category'), failed=document.querySelector('#only-failed');
function syncExpand(){const visible=items.filter(x=>!x.hidden),button=document.querySelector('#expand');button.disabled=visible.length===0;button.textContent=visible.some(x=>!x.open)?'展开全部':'收起全部';}
function filter(){let visible=0;for(const item of items){item.hidden=!(item.dataset.search.toLowerCase().includes(search.value.toLowerCase())&&(!category.value||item.dataset.category===category.value)&&(!failed.checked||item.dataset.status!=='passed'));if(!item.hidden)visible++;}document.querySelector('#visible-count').textContent=visible+' / '+items.length+' 项';syncExpand();}
search.addEventListener('input',filter);category.addEventListener('change',filter);failed.addEventListener('change',filter);
document.querySelector('#expand').addEventListener('click',()=>{const show=items.filter(x=>!x.hidden).some(x=>!x.open);for(const item of items)if(!item.hidden)item.open=show;syncExpand();});
for(const item of items)item.addEventListener('toggle',syncExpand);
