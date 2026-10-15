import { expect, it } from 'vitest'
import type { GraphLink, GraphNode } from '@/api/client'
import { hopKey, hopTarget, linkedInOrder, tourByLinks, tourByYear } from './paperWalk'

const link = (source: string, target: string, kind: GraphLink['kind']) => ({ source, target, kind }) as GraphLink
const paper = (id: string, year: number | null) => ({ id, title: id.toUpperCase(), year }) as GraphNode
const key = (k: string, mods: Partial<KeyboardEvent> = {}) =>
  hopKey({ key: k, ctrlKey: false, metaKey: false, altKey: false, ...mods })

it("lists a paper's linked papers in the side panel's order: by kind, then as they come, each once", () => {
  const links = [
    link('a', 'm', 'manual'),
    link('c', 'a', 'cites'),
    link('a', 's', 'similar'),
    link('a', 'c2', 'cites'),
    link('a', 'c', 'similar'), // c again, under another kind: listed once
    link('x', 'y', 'cites'),
  ]
  expect(linkedInOrder(links, 'a')).toEqual(['c', 'c2', 's', 'm'])
  expect(linkedInOrder(links, 'nobody')).toEqual([])
})

it('hops one step along the order, wrapping, and starts from the ends when the open paper is not in it', () => {
  const order = ['b', 'c', 'd']
  expect(hopTarget(order, null, 1)).toBe('b')
  expect(hopTarget(order, null, -1)).toBe('d')
  expect(hopTarget(order, 'b', 1)).toBe('c')
  expect(hopTarget(order, 'd', 1)).toBe('b')
  expect(hopTarget(order, 'b', -1)).toBe('d')
  expect(hopTarget(order, 'elsewhere', 1)).toBe('b')
  expect(hopTarget([], null, 1)).toBeNull()
})

it('steps on ] and [, never with Ctrl, Cmd or Alt', () => {
  expect(key(']')).toBe(1)
  expect(key('[')).toBe(-1)
  expect(key('a')).toBeNull()
  expect(key(']', { metaKey: true })).toBeNull()
  expect(key('[', { ctrlKey: true })).toBeNull()
  expect(key(']', { altKey: true })).toBeNull()
})

const nodes = [paper('a', 2020), paper('b', null), paper('c', 2010), paper('d', 2010), paper('e', 2030)]
const tourLinks = [
  link('a', 'b', 'cites'),
  link('b', 'c', 'cites'),
  link('a', 'd', 'similar'),
  link('c', 'd', 'manual'),
  link('b', 'd', 'cites'),
]

it('tours by links breadth first from the open paper, or from the most linked one, each paper once', () => {
  expect(tourByLinks(nodes, tourLinks, 'a')).toEqual(['a', 'b', 'd', 'c'])
  expect(tourByLinks(nodes, tourLinks, null)).toEqual(['b', 'a', 'c', 'd']) // b and d tie on 3 links; b is listed first
  expect(tourByLinks(nodes, tourLinks, 'gone')).toEqual(['b', 'a', 'c', 'd'])
  expect(tourByLinks([], [], null)).toEqual([])
})

it('tours oldest first, unknown years last, ties by title', () => {
  expect(tourByYear(nodes)).toEqual(['c', 'd', 'a', 'e', 'b'])
})
