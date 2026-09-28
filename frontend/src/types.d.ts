declare module 'bpmn-js/lib/NavigatedViewer' {
  export default class NavigatedViewer {
    constructor(options: {container: HTMLElement});
    importXML(xml: string): Promise<unknown>;
    get(name: string): any;
    destroy(): void;
  }
}

declare module '*.css';
