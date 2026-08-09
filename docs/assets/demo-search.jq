{
  count,
  item: (.matches[0] | {
    name: .model.name,
    ownership: .item.ownership_state,
    condition: .item.condition,
    location
  })
}
