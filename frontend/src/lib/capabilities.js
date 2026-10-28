export const canExecute = health => health?.capabilities?.execute === true
export const isViewer = health => health?.capabilities?.read_only === true
