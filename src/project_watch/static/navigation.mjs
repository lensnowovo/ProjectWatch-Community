const tabs = new Set(['overview','internal','external','changes','business','finance','monitoring','materials','review','report']);
export function primaryView(tab) {
  if (tab === 'overview') return 'overview';
  if (tab === 'materials') return 'materials';
  return ['external','changes'].includes(tab) ? 'external' : 'internal';
}
export function contentView(tab) { return ({external:'changes',monitoring:'business'})[tab] || tab; }
export function parseRoute(hash) {
  const [route, id, tab] = hash.replace(/^#\/?/, '').split('/');
  if (route === 'project' && /^[\w-]{1,100}$/.test(id || '')) return {route, projectId:id, tab:tabs.has(tab)?tab:'overview', panel:null};
  return {route:['home','projects','tasks','settings'].includes(route)?route:'home', projectId:null, panel:null};
}
export function routeHash(s) { return s.route === 'project' ? `#project/${s.projectId}/${s.tab}` : `#${s.route}`; }
export function reduceNavigation(s, action) {
  if (action.type === 'close-panel') return {...s,panel:null};
  if (action.type === 'project') return {route:'project',projectId:action.id,tab:'overview',panel:null};
  if (action.type === 'panel') return {...s,panel:action.panel};
  return s;
}
