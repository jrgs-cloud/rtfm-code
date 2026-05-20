import { Request, Response } from "express";

export interface Config {
    port: number;
    host: string;
}

export class Server extends EventEmitter {
    private port: number;

    constructor(config: Config) {
        super();
        this.port = config.port;
    }

    start(): void {
        this.listen();
    }

    private listen(): void {
        console.log("listening");
    }
}

export function createServer(config: Config): Server {
    return new Server(config);
}

const helper = (x: number): number => x + 1;
