package com.autoimmune.orders;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.ResponseStatus;

@ResponseStatus(HttpStatus.BAD_REQUEST)
public class MissingCustomerException extends RuntimeException {

    public MissingCustomerException() {
        super("customer parameter is required");
    }
}
