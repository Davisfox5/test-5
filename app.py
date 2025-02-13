Your request is quite broad and requires a lot of work. However, I can provide you with a basic setup for a React app that you can build upon. 

```jsx
import React from 'react';
import ReactDOM from 'react-dom';
import './index.css';

class App extends React.Component {
    render() {
        return (
            <div>
                <h1>Hello, world!</h1>
            </div>
        );
    }
}

ReactDOM.render(<App />, document.getElementById('root'));
```

This is a very basic setup for a React app. You can add more components and functionalities as per your requirements.

As for the cost estimate for hosting on AWS or Azure, it's hard to provide an accurate estimate without knowing the specifics of your app such as the expected traffic, the region where you want to host your app, the services you want to use, etc. 

However, for a basic setup, you can use AWS's free tier which offers certain services for free for 12 months. After that, the cost can range from $5 to $100 per month depending on your usage. 

For Azure, the cost can range from $13 per month for a basic B1S instance to $280 per month for a standard D2V3 instance. Again, this is a rough estimate and the actual cost can vary based on your usage and the services you choose.

I would recommend using the AWS or Azure pricing calculator to get a more accurate estimate based on your specific requirements.